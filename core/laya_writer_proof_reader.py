"""Restricted warm RO proof readers with explicit native lifetime ownership.

Cold/no-aux admission remains refused. These readers grant no normal writer,
cohort or immutable capability and are not connected to the host query path.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import weakref

import laya_journal_reader as R
import laya_reader_windows as W
import laya_writer_open_plan as P
from laya_index_sync import validate_sync_owner

_owners = {}
_retained = {}
_SIMPLE_PRAGMAS = frozenset(('application_id', 'user_version', 'schema_version',
                            'data_version', 'query_only', 'trusted_schema', 'journal_mode'))
_TABLE_PRAGMAS = frozenset(('table_info', 'table_xinfo', 'index_list', 'index_info',
                           'index_xinfo', 'foreign_key_list'))


def _files(path, state):
    files = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        name = Path(str(path) + suffix)
        if not os.path.lexists(name):
            files[suffix] = None
        else:
            R._file(name, state)
            info = name.lstat()
            files[suffix] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)
    return files


def _sql_allowed(sql):
    if type(sql) is not str:
        return False
    text = sql.strip()
    if re.match(r'(?is)^SELECT\b', text) or text.rstrip(';').upper() == 'BEGIN':
        return True
    match = re.fullmatch(r'(?is)PRAGMA\s+(?:main\.)?([a-z_]+)(?:\(([^;()]*)\))?\s*;?', text)
    return bool(match and ((match[1].lower() in _SIMPLE_PRAGMAS and match[2] is None)
                          or (match[1].lower() in _TABLE_PRAGMAS and match[2] is not None)))


class _ProofExitStack(ExitStack):
    """Retain every callback while a native connection still needs cleanup."""
    def __init__(self):
        super().__init__()
        self.record = None
        self.owners = {}
        self.expired = False
        self.invalid = False
        self.released = False
        self.pid, self.thread = os.getpid(), threading.current_thread()

    def __exit__(self, exc_type, exc, traceback):
        if self.released:
            return False
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_owner_changed')
        self.expired = True
        error = None
        for owner in list(self.owners.values()):
            try:
                owner.close()
            except Exception as cause:
                error = cause
        if self.owners:
            _retained[id(self)] = self
            raise W.ReaderCloseDeferred('writer_proof_close_deferred') from error
        self.released = True
        _retained.pop(id(self), None)
        self.record = None
        return super().__exit__(exc_type, exc, traceback)


class _Owner:
    def __init__(self, record, role):
        self.scope = record['stack']
        self.role = role
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.db = None
        self.pending = False
        self.transaction = None  # native cursor machinery never adopts DELETE writes
        self.cleaning = False
        _packed, snapshot = P._plan_record(record['plan'])
        self.snapshot = snapshot
        row = snapshot['components'][role]
        self.path, self.node = Path(row['path']), row['node']
        self.state = Path(snapshot['endpoint']['state'])
        self.before = _files(self.path, self.state)
        R._need(list(self.before[''][:2]) == self.node, 'writer_proof_component_changed')
        # Normal RO can create WAL auxiliaries on a cold image. This limited
        # opener only accepts an already present, private warm pair.
        if self.before['-wal'] is None or self.before['-shm'] is None:
            raise W.ReaderWindowBusy('writer_proof_cold_open_unproved')
        R._need(self.before['-journal'] is None, 'writer_proof_recovery_unproved')
        self.scope.owners[id(self)] = self
        _owners[id(self)] = self

    def thread_check(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_connection_owner_changed')

    def sql_check(self):
        self.thread_check()
        if self.db is None or self.db._native_closed:
            raise sqlite3.ProgrammingError('Cannot operate on a closed database.')
        R._need(not self.pending and not self.scope.expired and not self.scope.invalid,
                'writer_proof_reader_expired')
        record = self.scope.record
        R._need(record is not None and P._proof_window_record(record['window']) is record,
                'writer_proof_reader_expired')
        try:
            validate_sync_owner(record['queue_owner'], record['plan'].queue_path)
        except Exception:
            self.scope.invalid = True
            raise
        if time.monotonic() >= record['deadline']:
            raise W.ReaderWindowBusy('writer_proof_deadline')

    def cleanup_check(self):
        self.thread_check()
        record = self.scope.record
        R._need(record is not None and not self.scope.released, 'writer_proof_windows_changed')
        lifetime = record['lifetime']
        lifetime.check()
        W._named(record['plan'].install_id, lifetime.parent, lifetime.parent_fd,
                 lifetime.parent_info, self.role + '-physical.lock', record['physical'][self.role])
        R._need(R._node(Path(self.snapshot['project'])) == self.snapshot['endpoint']['project_node']
                and R._node(self.state) == self.snapshot['endpoint']['state_node']
                and _files(self.path, self.state) == self.before, 'writer_proof_component_changed')

    def authorize(self, action, first, second, database, trigger):
        if self.cleaning and action == sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_OK
        try:
            self.sql_check()
        except Exception:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            name = (first or '').lower()
            allowed = ((name in _SIMPLE_PRAGMAS and second is None)
                       or name in _TABLE_PRAGMAS)
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (second or first or '').lower() == 'load_extension':
            return sqlite3.SQLITE_DENY
        allowed = (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_TRANSACTION, getattr(sqlite3, 'SQLITE_RECURSIVE', -1))
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    def interrupt(self):
        if self.cleaning:
            return 0
        record = self.scope.record
        if record is None or self.scope.expired or time.monotonic() >= record['deadline']:
            return 1
        try:
            from laya_index_sync import _lease_record
            _lease_record(record['queue_owner'])
            P._proof_window_record(record['window'])
        except Exception:
            return 1
        return 0

    def open(self):
        record = self.scope.record
        try:
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                timeout=min(2., max(0., record['deadline'] - time.monotonic())),
                isolation_level=None, factory=W._OwnedSQLite)
            # Strong registration and the original window precede the first
            # Python initialization statement, including a failing PRAGMA.
            self.db._window_owner = weakref.ref(self)
            for sql in ('PRAGMA query_only=ON', 'PRAGMA trusted_schema=OFF'):
                self.sql_check()
                cursor = self.db.execute(sql)
                try:
                    cursor.fetchall()
                finally:
                    cursor.close()
            self.db.set_authorizer(self.authorize)
            self.db.set_progress_handler(self.interrupt, 1000)
            R._need(_files(self.path, self.state) == self.before, 'writer_proof_component_changed')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.thread_check()
        if self.db is not None:
            self.pending = True
            try:
                self.cleanup_check()
                self.cleaning = True
                self.db._native_close_locked()
            except Exception as exc:
                self.scope.invalid = True
                raise W.ReaderCloseDeferred('writer_proof_close_deferred') from exc
            finally:
                self.cleaning = False
            self.db = None
        self.pending = False
        self.scope.owners.pop(id(self), None)
        _owners.pop(id(self), None)


def _close(owner):
    owner.close()
    if owner.scope.expired and not owner.scope.owners:
        owner.scope.__exit__(None, None, None)


class _Cursor:
    __slots__ = ('_owner', '_cursor')
    def __init__(self, owner, cursor):
        self._owner, self._cursor = owner, cursor
    def __iter__(self):
        return self
    def __next__(self):
        self._owner.sql_check()
        return next(self._cursor)
    def fetchone(self):
        self._owner.sql_check()
        return self._cursor.fetchone()
    def fetchmany(self, *args):
        self._owner.sql_check()
        return self._cursor.fetchmany(*args)
    def fetchall(self):
        self._owner.sql_check()
        return self._cursor.fetchall()
    def close(self):
        self._owner.thread_check()
        if not self._cursor._window_closed:
            self._owner.cleanup_check()
            self._cursor.close()


class _Reader:
    __slots__ = ('_owner',)
    def __init__(self, owner):
        self._owner = owner
    def execute(self, sql, parameters=()):
        self._owner.sql_check()
        R._need(_sql_allowed(sql), 'writer_proof_sql_refused')
        return _Cursor(self._owner, self._owner.db.execute(sql, parameters))
    def commit(self):
        self._owner.sql_check()
        self._owner.db.commit()
    def rollback(self):
        self._owner.thread_check()
        if self._owner.db is None:
            raise sqlite3.ProgrammingError('Cannot operate on a closed database.')
        self._owner.cleanup_check()
        self._owner.cleaning = True
        try:
            self._owner.db.rollback()
        finally:
            self._owner.cleaning = False
    def close(self):
        _close(self._owner)


class _ReaderContext:
    """Explicit owner only; discarding this object cannot close a native DB."""
    def __init__(self, window, role):
        self.window, self.role, self.owner = window, role, None
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.entered = False
    def __enter__(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_connection_owner_changed')
        R._need(not self.entered, 'writer_proof_reader_reentered')
        self.entered = True
        P.validate_writer_proof_window(self.window)
        R._need(type(self.role) is str and self.role in ('base', 'queue'), 'writer_proof_role_refused')
        self.owner = _Owner(P._proof_window_record(self.window), self.role)
        self.owner.open()
        return _Reader(self.owner)
    def __exit__(self, exc_type, exc, traceback):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'writer_proof_connection_owner_changed')
        if self.owner is not None:
            _close(self.owner)
        return False


def proof_reader(window, role):
    return _ReaderContext(window, role)


def _scopes():
    scopes = dict(_retained)
    scopes.update((id(owner.scope), owner.scope) for owner in list(_owners.values()))
    scopes.update((id(record['stack']), record['stack']) for record in list(P._proofs.values()))
    return [scope for scope in scopes.values() if scope.pid == os.getpid() and not scope.released]


def _expired(scope):
    if scope.expired or scope.invalid or scope.record is None:
        return True
    record = scope.record
    if time.monotonic() >= record['deadline']:
        return True
    # Inspect issued lifetime without applying the observing thread's identity
    # to another creator's lease. Only its creator may perform native cleanup.
    from laya_index_sync import _sync_leases
    lease = _sync_leases.get(record['queue_owner'])
    return lease is None or lease['pid'] != scope.pid or lease['thread'] is not scope.thread


def pending_proof_readers():
    return sum(_expired(scope) or any(owner.pending for owner in list(scope.owners.values()))
               for scope in _scopes())


def drain_proof_readers(*, deadline):
    W._deadline(deadline)
    for scope in _scopes():
        if scope.thread is not threading.current_thread():
            continue
        if time.monotonic() >= deadline:
            break
        if _expired(scope):
            record = scope.record
            try:
                if record is not None and not record['context'].finished:
                    record['context'].__exit__(None, None, None)
                else:
                    scope.__exit__(None, None, None)
            except Exception:
                # Authority expiry may be the reason for this drain. A real
                # native cleanup failure remains retained and must propagate.
                if not scope.released:
                    raise
        else:
            for owner in list(scope.owners.values()):
                if time.monotonic() >= deadline:
                    break
                if owner.pending:
                    _close(owner)
    return pending_proof_readers()
