"""Explicit component connection windows and same-thread close ownership.

These optional v6 boundaries do not attest a complete writer cohort or grant
immutable reads. Preparation needs the existing installer and real mover owner;
normal participants open the stable UID locks without creating them.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
import weakref

import laya_journal_reader as R
import laya_reader_lease as L

_ROLES = ('base', 'binding', 'catalog', 'queue', 'sidecar')
_WINDOW_NAMES = ('publish-cut.lock',) + tuple(f'{role}-{kind}.lock'
    for role in _ROLES for kind in ('mutation', 'physical'))
_MANIFEST = 'component-windows-v1.json'
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


def _named(install_id, parent, parent_fd, parent_info, name, fd, *, links=1):
    R._need(R._lock_dir(install_id, create=False) == parent
            and os.path.samestat(parent_info, parent.lstat())
            and os.path.samestat(parent_info, os.fstat(parent_fd)),
            'reader_window_changed')
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    for info in (opened, named):
        if links == 1:
            L._private_file(info)
        else:
            # Only the explicitly recovered manifest publication accepts two
            # names. Component and lifetime locks always require one link.
            R._need(links == 2 and stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid() and info.st_nlink == 2
                    and stat.S_IMODE(info.st_mode) == 0o600,
                    'reader_windows_invalid')
    R._need(os.path.samestat(opened, named), 'reader_window_changed')


def _manifest(install_id, parent, parent_fd, parent_info, *, links=1):
    """Completed preparation commits every stable lock inode, without repair."""
    fd = os.open(_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd, links=links)
        raw = os.read(fd, 65537)
        R._need(len(raw) <= 65536, 'reader_windows_invalid')
        document = json.loads(raw)
        R._need(type(document) is dict and set(document) == {'version', 'install_id', 'locks'}
                and type(document['version']) is int and document['version'] == 1
                and document['install_id'] == install_id
                and type(document['locks']) is dict
                and set(document['locks']) == {*_WINDOW_NAMES, 'reader-lifetime.lock'}
                and R._raw(document) == raw, 'reader_windows_invalid')
        for name, node in document['locks'].items():
            R._need(type(node) is list and len(node) == 2
                    and all(type(value) is int and value >= 0 for value in node),
                    'reader_windows_invalid')
            lock = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                _named(install_id, parent, parent_fd, parent_info, name, lock)
                info = os.fstat(lock)
                R._need(node == [info.st_dev, info.st_ino], 'reader_window_changed')
            finally:
                os.close(lock)
        _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd, links=links)
        return raw
    finally:
        os.close(fd)


def _prepare_initial(install_id, parent, parent_fd, parent_info):
    nodes = {}
    for name in _WINDOW_NAMES:
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
        try:
            _named(install_id, parent, parent_fd, parent_info, name, fd)
            os.fsync(fd)
            info = os.fstat(fd)
            nodes[name] = [info.st_dev, info.st_ino]
        finally:
            os.close(fd)
    info = os.stat('reader-lifetime.lock', dir_fd=parent_fd, follow_symlinks=False)
    nodes['reader-lifetime.lock'] = [info.st_dev, info.st_ino]
    raw = R._raw(dict(version=1, install_id=install_id, locks=nodes))
    temporary = '.component-windows-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent_fd)  # lock names and the private temp precede commit
        # No-clobber commit. A crash before this point has no complete marker;
        # lifetime EX excludes all live participants before any retry creates.
        os.link(temporary, _MANIFEST, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _recover_manifest_link(install_id, parent, parent_fd, parent_info):
    """Recover only the proved, unique temp alias of a complete publication.

    The caller holds lifetime EX. Unknown hardlinks or invalid contents are
    refused; neither the manifest inode nor any coordination lock is replaced.
    """
    _manifest(install_id, parent, parent_fd, parent_info, links=2)
    fd = os.open(_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd, links=2)
        info = os.fstat(fd)
        aliases = []
        for name in os.listdir(parent_fd):
            if re.fullmatch(r'\.component-windows-[0-9a-f]{32}', name):
                node = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if os.path.samestat(info, node):
                    aliases.append(name)
        R._need(len(aliases) == 1, 'reader_windows_recovery_unproved')
        name = aliases[0]
        temporary = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            _named(install_id, parent, parent_fd, parent_info, name, temporary, links=2)
            R._need(os.path.samestat(info, os.fstat(temporary)), 'reader_windows_recovery_unproved')
            _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd, links=2)
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd)
        finally:
            os.close(temporary)
    finally:
        os.close(fd)
    _manifest(install_id, parent, parent_fd, parent_info)


def prepare_windows(installer):
    """Prepare locks, without claiming old owners or all writers are quiesced."""
    endpoint, _scope = _owner(installer)
    R._recheck(endpoint)
    record = endpoint.record
    row = L._read_v5_bundle(Path(record['state']) / 'install-journal.sqlite')
    R._need(row['pending'] is None and row['state'] in ('active', 'restored_old')
            and row['selected']['installId'] == record['install_id'],
            'reader_install_changed')
    install_id = record['install_id']
    parent, parent_fd, parent_info = L._parent(install_id, create=False)
    try:
        # A completed namespace is never repaired, even if a lock is missing.
        if os.path.lexists(parent / _MANIFEST):
            if os.stat(_MANIFEST, dir_fd=parent_fd, follow_symlinks=False).st_nlink == 2:
                with L.reader_lifetime(install_id, exclusive=True):
                    # Another preparer may have completed before this EX.
                    if os.stat(_MANIFEST, dir_fd=parent_fd, follow_symlinks=False).st_nlink == 2:
                        _recover_manifest_link(install_id, parent, parent_fd, parent_info)
                    else:
                        _manifest(install_id, parent, parent_fd, parent_info)
            else:
                _manifest(install_id, parent, parent_fd, parent_info)
        else:
            with L.reader_lifetime(install_id, exclusive=True):
                if not os.path.lexists(parent / _MANIFEST):
                    _prepare_initial(install_id, parent, parent_fd, parent_info)
                _manifest(install_id, parent, parent_fd, parent_info)
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
    parent, parent_fd, parent_info = L._parent(record['install_id'], create=False)
    try:
        manifest = _manifest(record['install_id'], parent, parent_fd, parent_info)
    finally:
        os.close(parent_fd)
    _components[component] = (os.getpid(), installer, scope, tuple(node), manifest)
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
def _window(component, kind, *, exclusive, deadline, closing=False):
    binding = _component(component)
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
        if closing:
            # Existing handles need only their sealed lifetime/physical locks
            # to finish. Unrelated component damage cannot strand this close.
            sealed = json.loads(binding[4])['locks']
            for required, opened in (('reader-lifetime.lock', lifetime.fd), (name, fd)):
                info = os.fstat(opened)
                R._need(sealed[required] == [info.st_dev, info.st_ino], 'reader_window_changed')
        else:
            R._need(_manifest(component.install_id, lifetime.parent, lifetime.parent_fd,
                              lifetime.parent_info) == binding[4], 'reader_windows_changed')
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


def _connect_owned(component, mode):
    lifetime = _Lifetime(component.install_id)
    try:
        db = sqlite3.connect(component.path.as_uri() + '?mode=' + mode, uri=True,
                             timeout=2, isolation_level=None)
    except BaseException:
        lifetime.close()
        raise
    owner = _OwnedConnection(component, db, lifetime)
    _owned[id(owner)] = owner
    return owner


def open_owned(component, *, path, validate, initialize, deadline):
    """Open and initialize under EX; register before initialize's first PRAGMA."""
    # Until a managed worker's same-thread drain is integrated, reject before
    # opening in a background thread rather than strand a connection on exit.
    R._need(threading.current_thread() is threading.main_thread(),
            'reader_connection_main_thread_required')
    _pid, installer, scope, node, _sealed_manifest = _component(component)
    _endpoint, current = _owner(installer)
    R._need(current is scope and Path(path) == component.path,
            'reader_component_owner_changed')
    with physical_window(component, exclusive=True, deadline=deadline):
        # An issued seal alone is not current installer evidence. Protect the
        # base during this full recheck, before the fixed RW connect below.
        R._recheck(_endpoint)
        R._need(tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                'reader_component_changed')
        check = _connect_owned(component, 'ro')
        try:
            validate(check.db)
        finally:
            check.close_locked()
        owner = _connect_owned(component, 'rw')
        try:
            R._need(tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                    'reader_component_changed')
            initialize(owner.db)
        except BaseException:
            owner.close_locked()
            raise
        return owner


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
        with _window(owner.component, 'physical', exclusive=True, deadline=deadline, closing=True):
            owner.close_locked()
        return True
    except ReaderWindowBusy:
        return False


def _local_pending():
    return [owner for owner in list(_owned.values()) if owner.pending
            and owner.pid == os.getpid() and owner.thread is threading.current_thread()]


def pending_count():
    # Observation is process-wide; draining remains restricted to the owner.
    return sum(owner.pending and owner.pid == os.getpid() for owner in list(_owned.values()))


def drain_pending(*, deadline):
    _deadline(deadline)
    for owner in _local_pending():
        close_or_defer(owner, deadline=deadline)
    return pending_count()
