"""Explicit component connection windows and same-thread close ownership.

These optional v6 boundaries do not attest a complete writer cohort or grant
immutable reads. Preparation needs the existing installer and real mover owner;
normal participants open the stable UID locks without creating them.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import weakref

import laya_journal_reader as R
import laya_reader_lease as L

_ROLES = ('base', 'binding', 'catalog', 'queue', 'sidecar')
_components = weakref.WeakKeyDictionary()
# Strong ownership starts immediately after connect, before any initialization.
# No finalizer closes these handles. Only their creating thread may drain them.
_owned = {}


class ReaderWindowBusy(ValueError):
    pass


class ReaderCloseDeferred(RuntimeError):
    pass


@dataclass(frozen=True, eq=False, init=False)
class Component:
    install_id: str
    role: str
    path: Path

    def __init__(self):
        raise ValueError('reader_component_not_issued')


def _deadline(value):
    R._need(type(value) in (float, int) and math.isfinite(value),
            'reader_window_deadline_invalid')
    return value


def _owner(installer):
    from laya_move_read_seal import _owner as move_owner
    endpoint = R._installer(installer)
    owner = move_owner()
    record = endpoint.record
    R._need(record['capability'] == 'v6-ro-v1' and record['store_format'] == 'v6'
            and str(owner.project) == record['project']
            and str(owner.state) == record['state']
            and list(owner.project_node) == list(record['project_node'])
            and list(owner.state_node) == list(record['state_node']),
            'reader_component_owner_changed')
    return endpoint, owner


def _named(install_id, parent, parent_fd, parent_info, name, fd):
    R._need(R._lock_dir(install_id, create=False) == parent
            and os.path.samestat(parent_info, parent.lstat())
            and os.path.samestat(parent_info, os.fstat(parent_fd)),
            'reader_window_changed')
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    L._private_file(opened)
    L._private_file(named)
    R._need(os.path.samestat(opened, named), 'reader_window_changed')


def prepare_windows(installer):
    """Prepare locks, without claiming old owners or all writers are quiesced."""
    endpoint, _scope = _owner(installer)
    record = endpoint.record
    row = L._read_v5_bundle(Path(record['state']) / 'install-journal.sqlite')
    R._need(row['pending'] is None and row['state'] in ('active', 'restored_old')
            and row['selected']['installId'] == record['install_id'],
            'reader_install_changed')
    install_id = record['install_id']
    parent, parent_fd, parent_info = L._parent(install_id, create=False)
    try:
        names = ['publish-cut.lock'] + [f'{role}-{kind}.lock'
            for role in _ROLES for kind in ('mutation', 'physical')]
        for name in names:
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=parent_fd)
            try:
                _named(install_id, parent, parent_fd, parent_info, name, fd)
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def component_for(installer, role):
    endpoint, scope = _owner(installer)
    # Queue binding and the read-open bootstrap plan are separate integrations;
    # this issuer cannot invent a queue path absent from the endpoint record.
    keys = {'base': 'db', 'binding': 'binding', 'catalog': 'catalog', 'sidecar': 'sidecar'}
    R._need(type(role) is str and role in keys, 'reader_component_unsupported')
    key = keys[role]
    record = endpoint.record
    path = Path(record[key + '_path'])
    node = record.get(key + '_node')
    R._need(node is not None, 'reader_component_identity_missing')
    component = object.__new__(Component)
    object.__setattr__(component, 'install_id', record['install_id'])
    object.__setattr__(component, 'role', role)
    object.__setattr__(component, 'path', path)
    _components[component] = (os.getpid(), installer, scope, tuple(node))
    return component


def _component(component):
    R._need(type(component) is Component and component in _components,
            'reader_component_not_issued')
    binding = _components[component]
    R._need(binding[0] == os.getpid(), 'reader_component_expired')
    return binding


class _Lifetime:
    """An independent SH descriptor, never borrowed from a caller context."""
    def __init__(self, install_id):
        self.install_id = install_id
        self.parent, self.parent_fd, self.parent_info = L._parent(install_id, create=False)
        self.fd = None
        try:
            self.fd = os.open('reader-lifetime.lock', os.O_RDONLY | os.O_NONBLOCK |
                              os.O_NOFOLLOW, dir_fd=self.parent_fd)
            L._private_file(os.fstat(self.fd))
            try:
                fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReaderWindowBusy('reader_lifetime_busy') from exc
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        L._named(self.install_id, self.parent, self.parent_fd, self.parent_info, self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None


@contextmanager
def _window(component, kind, *, exclusive, deadline):
    _component(component)
    R._need(type(exclusive) is bool, 'reader_window_mode_invalid')
    _deadline(deadline)
    lifetime = _Lifetime(component.install_id)
    fd = None
    try:
        name = f'{component.role}-{kind}.lock'
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=lifetime.parent_fd)
        L._private_file(os.fstat(fd))
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReaderWindowBusy(f'reader_{kind}_busy') from exc
                time.sleep(min(0.005, remaining))
        _named(component.install_id, lifetime.parent, lifetime.parent_fd,
               lifetime.parent_info, name, fd)
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lifetime.close()


def physical_window(component, *, exclusive, deadline):
    """A lock only; possession grants no store, installer or writer authority."""
    return _window(component, 'physical', exclusive=exclusive, deadline=deadline)


def mutation_window(component, *, exclusive, deadline):
    return _window(component, 'mutation', exclusive=exclusive, deadline=deadline)


class _OwnedConnection:
    def __init__(self, component, db, lifetime):
        self.component, self.db, self.lifetime = component, db, lifetime
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.pending = False

    def check(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'reader_connection_owner_changed')

    def close_locked(self):
        # A failing SQLite close remains strongly owned and pending as well.
        self.pending = True
        self.db.close()
        self.db = None
        self.lifetime.close()
        _owned.pop(id(self), None)
        self.pending = False


def open_owned(component, *, path, opener, initialize, deadline):
    """Open and initialize under EX; register before initialize's first PRAGMA."""
    _pid, installer, scope, node = _component(component)
    _endpoint, current = _owner(installer)
    R._need(current is scope and Path(path) == component.path,
            'reader_component_owner_changed')
    lifetime = _Lifetime(component.install_id)
    owner = None
    try:
        with physical_window(component, exclusive=True, deadline=deadline):
            R._need(tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                    'reader_component_changed')
            db = opener()
            owner = _OwnedConnection(component, db, lifetime)
            _owned[id(owner)] = owner
            try:
                R._need(isinstance(db, sqlite3.Connection), 'reader_connection_invalid')
                initialize(db)
            except BaseException:
                owner.close_locked()
                raise
        return owner
    except BaseException:
        if owner is None:
            lifetime.close()
        raise


def close_or_defer(owner, *, deadline):
    R._need(type(owner) is _OwnedConnection, 'reader_connection_not_owned')
    owner.check()
    if owner.db is None:
        return True
    R._need(_owned.get(id(owner)) is owner, 'reader_connection_not_owned')
    owner.pending = True
    R._need(not owner.db.in_transaction, 'reader_connection_transaction_active')
    owner.lifetime.check()
    try:
        with physical_window(owner.component, exclusive=True, deadline=deadline):
            owner.close_locked()
        return True
    except ReaderWindowBusy:
        return False


def _local_pending():
    return [owner for owner in list(_owned.values()) if owner.pending
            and owner.pid == os.getpid() and owner.thread is threading.current_thread()]


def pending_count():
    return len(_local_pending())


def drain_pending(*, deadline):
    _deadline(deadline)
    for owner in _local_pending():
        close_or_defer(owner, deadline=deadline)
    return pending_count()
