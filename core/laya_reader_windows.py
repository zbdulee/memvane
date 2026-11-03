"""Explicit component connection windows and same-thread close ownership.

These optional v6 boundaries do not attest a complete writer cohort or grant
immutable reads. Preparation needs the existing installer and real mover owner;
normal participants open the stable UID locks without creating them.
"""
from contextlib import contextmanager, ExitStack
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
        R._need(type(document) is dict and set(document) == {'version', 'install_id', 'locks', 'temporary'}
                and type(document['version']) is int and document['version'] == 1
                and document['install_id'] == install_id
                and type(document['temporary']) is str
                and re.fullmatch(r'\.component-windows-[0-9a-f]{32}', document['temporary']) is not None
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
    temporary = '.component-windows-' + uuid.uuid4().hex
    raw = R._raw(dict(version=1, install_id=install_id, locks=nodes, temporary=temporary))
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
    raw = _manifest(install_id, parent, parent_fd, parent_info, links=2)
    fd = os.open(_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        _named(install_id, parent, parent_fd, parent_info, _MANIFEST, fd, links=2)
        info = os.fstat(fd)
        # A matching filename pattern does not prove it is our temporary
        # publication name. Recover only the exact name committed in the bytes.
        name = json.loads(raw)['temporary']
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
                    R._recheck(endpoint)
                    # Another preparer may have completed before this EX.
                    if os.stat(_MANIFEST, dir_fd=parent_fd, follow_symlinks=False).st_nlink == 2:
                        _recover_manifest_link(install_id, parent, parent_fd, parent_info)
                    else:
                        _manifest(install_id, parent, parent_fd, parent_info)
            else:
                with _proof_windows(component_for(installer, 'base'), deadline=time.monotonic() + 2):
                    R._recheck(endpoint)
                    _manifest(install_id, parent, parent_fd, parent_info)
        else:
            with L.reader_lifetime(install_id, exclusive=True):
                R._recheck(endpoint)
                if not os.path.lexists(parent / _MANIFEST):
                    _prepare_initial(install_id, parent, parent_fd, parent_info)
                _manifest(install_id, parent, parent_fd, parent_info)
    finally:
        os.close(parent_fd)


def _location(endpoint, role):
    record = endpoint.record
    if role == 'sidecar':
        path = Path(record['sidecar_path'])
        # Absence is a locking descriptor, not authority to create a database.
        return path, (tuple(R._file(path, Path(record['state'])))
                      if os.path.lexists(path) else None)
    if role == 'queue':
        # The selected route, not a caller's path, locates this component.
        # open_owned repeats the full current proof and this exact comparison.
        decision = R.journal.select_route(record['project'], action='recall')
        R._need(type(decision.get('queuePath')) is str, 'reader_component_changed')
        path = Path(decision['queuePath'])
        return path, tuple(R._file(path, Path(record['state'])))
    keys = {'base': 'db', 'binding': 'binding', 'catalog': 'catalog', 'sidecar': 'sidecar'}
    R._need(type(role) is str and role in keys, 'reader_component_unsupported')
    key = keys[role]
    path = Path(record[key + '_path'])
    node = record.get(key + '_node')
    R._need(node is not None, 'reader_component_identity_missing')
    return path, tuple(node)


def component_for(installer, role):
    endpoint, scope = _owner(installer)
    path, node = _location(endpoint, role)
    record = endpoint.record
    component = object.__new__(Component)
    object.__setattr__(component, 'install_id', record['install_id'])
    object.__setattr__(component, 'role', role)
    object.__setattr__(component, 'path', path)
    parent, parent_fd, parent_info = L._parent(record['install_id'], create=False)
    try:
        manifest = _manifest(record['install_id'], parent, parent_fd, parent_info)
    finally:
        os.close(parent_fd)
    _components[component] = (os.getpid(), installer, scope, node, manifest)
    return component


def _component(component):
    R._need(type(component) is Component and component in _components,
            'reader_component_not_issued')
    binding = _components[component]
    R._need(binding[0] == os.getpid(), 'reader_component_expired')
    return binding


def validate_runtime_components(components, *, paths, scope, incarnation):
    """Validate every explicit entry before any runtime SQLite connection.

    Missing entries retain the common engine's optional-boundary behavior;
    an explicit None or a component for another role cannot opt out of a lock.
    """
    installs = set()
    for role, component in components.items():
        binding = _component(component)
        expected = paths.get(role)
        R._need(expected is not None and component.role == role
                and component.path == Path(expected), 'invalid_runtime_connection_windows')
        endpoint, owner = _owner(binding[1])
        record = endpoint.record
        R._need(owner is binding[2] and record['scope'] == scope
                and record['incarnation'] == incarnation
                and record['db_path'] == paths['base'], 'invalid_runtime_connection_windows')
        installs.add(component.install_id)
    R._need(len(installs) == 1, 'invalid_runtime_connection_windows')


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
def _window(component, kind, *, exclusive, deadline, closing=False, proof_role=None):
    binding = _component(component)
    R._need(type(exclusive) is bool, 'reader_window_mode_invalid')
    _deadline(deadline)
    lifetime = _Lifetime(component.install_id)
    fd = None
    try:
        R._need(proof_role is None or (proof_role in _ROLES and kind == 'physical' and not closing),
                'reader_window_mode_invalid')
        role = component.role if proof_role is None else proof_role
        R._need(kind in ('physical', 'mutation', 'publish'), 'reader_window_mode_invalid')
        name = 'publish-cut.lock' if kind == 'publish' else f'{role}-{kind}.lock'
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


@contextmanager
def _proof_windows(component, *, deadline):
    # A full installer recheck reads all five component namespaces, including
    # sidecar absence. No content connection precedes these sorted EX locks.
    # The role override grants locks only, never another component's RW open.
    with ExitStack() as stack:
        for role in sorted(_ROLES):
            stack.enter_context(_window(component, 'physical', exclusive=True,
                                        deadline=deadline, proof_role=role))
        yield


def physical_window(component, *, exclusive, deadline):
    """A lock only; possession grants no store, installer or writer authority."""
    return _window(component, 'physical', exclusive=exclusive, deadline=deadline)


def mutation_window(component, *, exclusive, deadline):
    return _window(component, 'mutation', exclusive=exclusive, deadline=deadline)


def publish_window(component, *, exclusive, deadline):
    return _window(component, 'publish', exclusive=exclusive, deadline=deadline)


class _OwnedSQLite(sqlite3.Connection):
    """Track cursors issued by the managed connection's supported methods.

    The product uses cursor/execute/executemany/executescript. Direct native
    constructors such as sqlite3.Cursor(db) and base-class method bypasses are
    outside this cooperative API; this is not an arbitrary-native-API seal.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._live_cursors = weakref.WeakValueDictionary()
        self._closing_cursors = False
        self._native_closed = False

    def _cursor_allowed(self):
        if self._closing_cursors:
            raise sqlite3.ProgrammingError('reader_connection_closing')

    def _track_cursor(self, cursor):
        self._live_cursors[id(cursor)] = cursor
        return cursor

    def cursor(self, *args, **kwargs):
        self._cursor_allowed()
        return self._track_cursor(super().cursor(*args, **kwargs))

    def execute(self, *args, **kwargs):
        self._cursor_allowed()
        return self._track_cursor(super().execute(*args, **kwargs))

    def executemany(self, *args, **kwargs):
        self._cursor_allowed()
        return self._track_cursor(super().executemany(*args, **kwargs))

    def executescript(self, *args, **kwargs):
        self._cursor_allowed()
        return self._track_cursor(super().executescript(*args, **kwargs))

    def close(self):
        # The public raw handle must obey the same EX/deferred-owner contract.
        # A repeated close after its owner completed is SQLite's usual no-op.
        if not close_connection(self):
            R._need(self._native_closed, 'reader_connection_not_owned')

    def _native_close_locked(self):
        # sqlite3_close_v2 can otherwise return before outstanding statements
        # finalize. Reset every issued live cursor before closing the cache and
        # native handle. Any failure leaves the strong owner/lifetime pending.
        self._closing_cursors = True
        try:
            cursors = list(self._live_cursors.values())
            for cursor in cursors:
                cursor.close()
                # A custom factory may implement close as a pool return. Its
                # successful override is not evidence of native reset. Keep
                # failures pending, but confirm actual reset after success.
                sqlite3.Cursor.close(cursor)
            super().close()
            self._native_closed = True
            self._live_cursors.clear()
        finally:
            self._closing_cursors = False


class _OwnedConnection:
    def __init__(self, component, db, lifetime):
        self.component, self.db, self.lifetime = component, db, lifetime
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.pending = False
        self.transaction = None
        self.auxiliaries = None

    def check(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'reader_connection_owner_changed')

    def close_locked(self):
        # A failing SQLite close remains strongly owned and pending as well.
        self.pending = True
        self.db._native_close_locked()
        self.db = None
        self.lifetime.close()
        _owned.pop(id(self), None)
        self.pending = False


def _connect_owned(component, mode):
    lifetime = _Lifetime(component.install_id)
    try:
        db = sqlite3.connect(component.path.as_uri() + '?mode=' + mode, uri=True,
                             timeout=2, isolation_level=None, factory=_OwnedSQLite)
    except BaseException:
        lifetime.close()
        raise
    owner = _OwnedConnection(component, db, lifetime)
    _owned[id(owner)] = owner
    try:
        if mode == 'rw':
            # Automatic checkpoints cannot run under an ordinary mutation SH.
            # Explicit checkpoint admission is a separate mutation/physical EX.
            R._need(db.execute('PRAGMA wal_autocheckpoint=0').fetchone() == (0,),
                    'reader_autocheckpoint_unavailable')
    except BaseException:
        owner.close_locked()
        raise
    return owner


class _WriteTransaction:
    def __init__(self, owner):
        self.owner = owner
        self.locks = ExitStack()
        self.rollback_pending = False

    def _check(self):
        self.owner.check()
        R._need(self.owner.transaction is self, 'reader_transaction_expired')

    def _finish(self):
        R._need(not self.owner.db.in_transaction, 'reader_connection_transaction_active')
        self.locks.close()
        self.owner.transaction = None
        self.rollback_pending = False

    def commit(self):
        self._check()
        R._need(not self.rollback_pending, 'reader_transaction_rollback_pending')
        self.owner.db.commit()
        self._finish()

    def rollback(self):
        self._check()
        try:
            self.owner.db.rollback()
            self._finish()
        except BaseException:
            # Neither the SQL transaction nor its logical locks may escape
            # through generator/finalizer cleanup. drain_pending retries on
            # this same creating thread before it attempts physical close.
            self.rollback_pending = True
            self.owner.pending = True
            raise


@contextmanager
def write_transaction(owner, *, visible, deadline):
    """Own mutation/cut locks and SQL completion as one explicit lifetime."""
    R._need(type(owner) is _OwnedConnection and _owned.get(id(owner)) is owner,
            'reader_connection_not_owned')
    owner.check()
    owner.lifetime.check()
    R._need(type(visible) is bool and owner.db is not None and not owner.pending
            and owner.transaction is None and not owner.db.in_transaction,
            'reader_transaction_unavailable')
    _deadline(deadline)
    _pid, installer, scope, _node, _manifest = _component(owner.component)
    _endpoint, current = _owner(installer)
    R._need(current is scope, 'reader_component_owner_changed')
    transaction = _WriteTransaction(owner)
    owner.transaction = transaction
    try:
        transaction.locks.enter_context(mutation_window(owner.component,
            exclusive=False, deadline=deadline))
        if visible:
            transaction.locks.enter_context(publish_window(owner.component,
                exclusive=True, deadline=deadline))
        R._need(time.monotonic() < deadline, 'reader_write_deadline')
        # A native SQLite writer can outlive our cooperative lock admission.
        # Limit that driver's busy wait to the remaining caller budget, then
        # recheck after BEGIN before any caller DML is allowed to run.
        previous = owner.db.execute('PRAGMA busy_timeout').fetchone()[0]
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        owner.db.execute(f'PRAGMA busy_timeout={min(previous, remaining_ms)}')
        try:
            owner.db.execute('BEGIN IMMEDIATE')
        finally:
            try:
                owner.db.execute(f'PRAGMA busy_timeout={previous}')
            except BaseException:
                owner.pending = True
                raise
        R._need(time.monotonic() < deadline, 'reader_write_deadline')
        yield transaction
        if owner.transaction is transaction:
            transaction.commit()
    except BaseException as error:
        if owner.transaction is transaction and not transaction.rollback_pending:
            try:
                transaction.rollback()
            except BaseException:
                if hasattr(error, 'add_note'):
                    error.add_note('reader SQL rollback remains strongly owned and pending')
        raise


def _auxiliary_nodes(path, state):
    result = {}
    for suffix in ('-wal', '-shm', '-journal'):
        auxiliary = Path(str(path) + suffix)
        result[suffix] = tuple(R._file(auxiliary, state)) if os.path.lexists(auxiliary) else None
    return result


def checkpoint_owned(owner, *, deadline):
    """Explicit writer-only WAL reset; no caller gains a clean-read capability.

    Busy/partial checkpoints may have advanced the main image. They are not a
    completed reset. The deadline bounds admission and the driver's busy wait,
    rather than promising a hard wall-clock bound on checkpoint filesystem I/O.
    """
    R._need(type(owner) is _OwnedConnection and _owned.get(id(owner)) is owner,
            'reader_connection_not_owned')
    owner.check()
    owner.lifetime.check()
    R._need(owner.db is not None and owner.auxiliaries is not None
            and not owner.pending and owner.transaction is None
            and not owner.db.in_transaction, 'reader_checkpoint_unavailable')
    _deadline(deadline)
    _pid, installer, scope, node, _manifest = _component(owner.component)
    R._need(node is not None, 'reader_component_absent')
    endpoint, current = _owner(installer)
    R._need(current is scope, 'reader_component_owner_changed')
    with mutation_window(owner.component, exclusive=True, deadline=deadline):
        with physical_window(owner.component, exclusive=True, deadline=deadline):
            path, current_node = _location(endpoint, owner.component.role)
            R._need(path == owner.component.path and current_node == node
                    and tuple(R._file(path, Path(endpoint.record['state']))) == node,
                    'reader_component_changed')
            # SQLite can mutate its already-open WAL/SHM before returning a
            # result. Reject shared/unsafe names before the first SQL call,
            # and bind completion to these same named file incarnations.
            auxiliaries = _auxiliary_nodes(path, Path(endpoint.record['state']))
            R._need(auxiliaries == owner.auxiliaries, 'reader_checkpoint_files_changed')
            if time.monotonic() >= deadline:
                raise ReaderWindowBusy('reader_checkpoint_deadline')
            previous = owner.db.execute('PRAGMA busy_timeout').fetchone()[0]
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            owner.db.execute(f'PRAGMA busy_timeout={min(previous, remaining_ms)}')
            try:
                if time.monotonic() >= deadline:
                    raise ReaderWindowBusy('reader_checkpoint_deadline')
                cursor = owner.db.execute('PRAGMA main.wal_checkpoint(TRUNCATE)')
                try:
                    rows = cursor.fetchall()
                finally:
                    cursor.close()
                    sqlite3.Cursor.close(cursor)
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                # Python 3.9 does not expose sqlite_errorcode. Keep its normal
                # driver busy/locked errors distinct from syntax/corruption.
                if ((type(code) is int and code & 0xff in (5, 6))
                        or 'locked' in str(exc).lower() or 'busy' in str(exc).lower()):
                    raise ReaderWindowBusy('reader_checkpoint_busy') from exc
                raise
            finally:
                try:
                    owner.db.execute(f'PRAGMA busy_timeout={previous}')
                except BaseException:
                    owner.pending = True
                    raise
            R._need(len(rows) == 1 and len(rows[0]) == 3
                    and all(type(value) is int for value in rows[0]),
                    'reader_checkpoint_result_invalid')
            result = tuple(rows[0])
            R._need(tuple(R._file(path, Path(endpoint.record['state']))) == node,
                    'reader_component_changed')
            R._need(_auxiliary_nodes(path, Path(endpoint.record['state'])) == auxiliaries,
                    'reader_checkpoint_files_changed')
            if time.monotonic() >= deadline:
                raise ReaderWindowBusy('reader_checkpoint_deadline')
            if result == (0, -1, -1):
                return dict(state='no_wal', checkpoint=result)
            if result != (0, 0, 0):
                raise ReaderWindowBusy('reader_checkpoint_busy')
            wal = Path(str(path) + '-wal')
            R._file(wal, Path(endpoint.record['state']))
            R._need(wal.lstat().st_size == 0, 'reader_checkpoint_result_invalid')
            return dict(state='reset_complete', checkpoint=result)


def open_owned(component, *, path, validate, initialize, deadline, preopen=None):
    """Open and initialize under EX; register before initialize's first PRAGMA."""
    # Until a managed worker's same-thread drain is integrated, reject before
    # opening in a background thread rather than strand a connection on exit.
    R._need(threading.current_thread() is threading.main_thread(),
            'reader_connection_main_thread_required')
    _pid, installer, scope, node, _sealed_manifest = _component(component)
    R._need(node is not None, 'reader_component_absent')
    _endpoint, current = _owner(installer)
    R._need(current is scope and Path(path) == component.path,
            'reader_component_owner_changed')
    with _proof_windows(component, deadline=deadline):
        R._need(tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                'reader_component_changed')
        if preopen is not None:
            preopen()
        # An issued seal alone is not current installer evidence. Protect the
        # base during this full recheck, before the fixed RW connect below.
        R._recheck(_endpoint)
        current_path, current_node = _location(_endpoint, component.role)
        R._need(current_path == component.path and current_node == node
                and tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                'reader_component_changed')
        check = _connect_owned(component, 'ro')
        try:
            validate(check.db)
        finally:
            check.close_locked()
        state = Path(_endpoint.record['state'])
        opening_auxiliaries = _auxiliary_nodes(component.path, state)
        owner = _connect_owned(component, 'rw')
        try:
            R._need(tuple(R._file(component.path, Path(_endpoint.record['state']))) == node,
                    'reader_component_changed')
            initialize(owner.db)
            R._need(not owner.db.in_transaction, 'reader_connection_transaction_active')
            # Force an actual main read while the creating EX is still held:
            # the WAL handle must be in use before we bind its named inode.
            # Existing names must span initialization unchanged; newly created
            # auxiliaries belong to this fixed SQLite open/read sequence.
            cursor = owner.db.execute('SELECT count(*) FROM sqlite_master')
            try:
                cursor.fetchall()
            finally:
                cursor.close()
                sqlite3.Cursor.close(cursor)
            auxiliaries = _auxiliary_nodes(component.path, state)
            R._need(all(before is None or auxiliaries[suffix] == before
                        for suffix, before in opening_auxiliaries.items()),
                    'reader_component_changed')
            owner.auxiliaries = auxiliaries
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
    R._need(owner.transaction is None, 'reader_connection_write_active')
    owner.lifetime.check()
    try:
        with _window(owner.component, 'physical', exclusive=True, deadline=deadline, closing=True):
            owner.close_locked()
        return True
    except ReaderWindowBusy:
        return False


def close_owned(owner, *, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + 2
    if not close_or_defer(owner, deadline=deadline):
        raise ReaderCloseDeferred('reader_connection_close_deferred')


def close_connection(db, *, deadline=None):
    """Close a registered raw connection without discarding deferred ownership.

    False means this module never owned the handle; exceptions from an owned
    close must propagate, so a caller cannot fall back to an unguarded close.
    """
    for owner in list(_owned.values()):
        if owner.db is db:
            close_owned(owner, deadline=deadline)
            return True
    return False


def create_sidecar_owned(component, *, identity=None, deadline):
    from laya_sidecar_create import create_sidecar_owned as create
    return create(component, identity=identity, deadline=deadline)


def _local_pending():
    return [owner for owner in list(_owned.values()) if owner.pending
            and owner.pid == os.getpid() and owner.thread is threading.current_thread()]


def pending_count():
    # Observation is process-wide; draining remains restricted to the owner.
    return sum(owner.pending and owner.pid == os.getpid() for owner in list(_owned.values()))


def drain_pending(*, deadline):
    _deadline(deadline)
    for owner in _local_pending():
        if owner.transaction is not None and owner.transaction.rollback_pending:
            owner.transaction.rollback()
        close_or_defer(owner, deadline=deadline)
    return pending_count()
