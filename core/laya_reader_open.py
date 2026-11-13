"""Warm reader connection ownership; not a complete snapshot capability.

Only an issued, lifetime-bound endpoint can open. A strong owner holds its
physical SH through native close, including failure. SQLite read-only WAL
opens may initialize their ordinary auxiliary files.
"""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

import laya_journal_reader as R
import laya_reader_lease as L
import laya_reader_windows as W

_owners = {}
_observations = ContextVar('laya_reader_observations', default=())
_FIELDS = {'base':'db', 'catalog':'catalog', 'binding':'binding', 'sidecar':'sidecar'}


class _ReaderSQLite(W._OwnedSQLite):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reader_authorizer = None
        self._snapshot_guards = set()
        self._reader_progress = (None, 0)
        self._progress_deadline = None

    def _install_reader_progress(self):
        callback, interval = self._reader_progress
        deadline = self._progress_deadline
        if deadline is None:
            super().set_progress_handler(callback, interval)
            return
        step = math.gcd(interval, 1000) if callback is not None and interval > 0 else 1000
        elapsed = 0
        def progress():
            nonlocal elapsed
            elapsed += step
            if callback is not None and interval > 0 and elapsed >= interval:
                elapsed %= interval
                if callback():
                    return 1
            return int(time.monotonic() >= deadline)
        super().set_progress_handler(progress, step)

    def set_progress_handler(self, callback, interval):
        self._reader_progress = (callback, interval)
        self._install_reader_progress()

    def _hold_progress_deadline(self, deadline):
        R._need(self._progress_deadline is None, 'reader_progress_already_owned')
        self._progress_deadline = deadline
        self._install_reader_progress()

    def _release_progress_deadline(self):
        self._progress_deadline = None
        self._install_reader_progress()

    def _install_reader_authorizer(self):
        external, guarded = self._reader_authorizer, bool(self._snapshot_guards)
        if external is None and not guarded and sys.version_info >= (3, 11):
            super().set_authorizer(None)
            return
        def authorize(action, arg1, arg2, database, source):
            if guarded and action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                return sqlite3.SQLITE_DENY
            return (sqlite3.SQLITE_OK if external is None else
                    external(action, arg1, arg2, database, source))
        super().set_authorizer(authorize)

    def set_authorizer(self, callback):
        self._reader_authorizer = callback
        self._install_reader_authorizer()

    def _hold_snapshot_transaction(self):
        R._need(self.in_transaction, 'reader_snapshot_unpinned')
        guard = object()
        self._snapshot_guards.add(guard)
        self._install_reader_authorizer()
        return guard

    def _release_snapshot_transaction(self, guard):
        R._need(guard in self._snapshot_guards, 'reader_snapshot_closed')
        self._snapshot_guards.remove(guard)
        self._install_reader_authorizer()

    def close(self):
        owner = _owners.get(id(self))
        if owner is None:
            R._need(self._native_closed, 'reader_connection_not_owned')
            return
        owner.close()


class _ReaderOwner:
    def __init__(self, db, locks):
        self.db, self.locks = db, locks
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.pending = False

    def close(self, *, deadline=None):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'reader_connection_owner_changed')
        if self.db is None:
            return
        if deadline is None:
            deadline = time.monotonic()+2
        W._deadline(deadline)
        R._need(_owners.get(id(self.db)) is self, 'reader_connection_not_owned')
        self.pending = True
        R._need(not self.db.in_transaction, 'reader_connection_transaction_active')
        R._need(time.monotonic() < deadline, 'reader_close_deadline')
        db = self.db
        if not db._native_closed:
            db._native_close_locked()
        self.locks.close()
        self.db = None
        _owners.pop(id(db),None)
        self.pending = False


def _bound(endpoint, *, capability='v6-ro-v1'):
    """Lifetime-bound read-only endpoint of one capability.

    Callers that open v6 stores (``inspect_warm_snapshot``) keep the default;
    a schema3 recall names ``R.SCHEMA3_CAPABILITY`` and never reaches them.
    """
    R._need(capability in ('v6-ro-v1', R.SCHEMA3_CAPABILITY), 'reader_endpoint_unsupported')
    R._need(type(endpoint) is R.VerifiedEndpoint and endpoint in R._issued
            and endpoint in R._read_only and endpoint in L._scoped_readers,
            'reader_endpoint_not_issued')
    scope = L._scoped_readers[endpoint]
    L._check_shared_scope(scope)
    R._need(scope in L._shared_stack.get() and endpoint.record['install_id'] == scope.install_id,
            'reader_lifetime_invalid')
    R._need(endpoint.record['capability'] == capability, 'reader_endpoint_unsupported')
    return scope


def _files(path, state):
    main = tuple(R._file(path,state))
    auxiliary = {}
    for suffix in ('-wal','-shm','-journal'):
        name=Path(str(path)+suffix)
        auxiliary[suffix] = tuple(R._file(name,state)) if os.path.lexists(name) else None
    return main,auxiliary


@contextmanager
def reader_publish_cut(endpoint, *, deadline):
    """Hold the cooperative publication cut; this grants no read capability."""
    W._deadline(deadline)
    _bound(endpoint)
    install_id=endpoint.record['install_id']
    lifetime=W._Lifetime(install_id)
    fd=None
    try:
        manifest=W._manifest(install_id,lifetime.parent,lifetime.parent_fd,lifetime.parent_info)
        fd=os.open('publish-cut.lock',os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,
                   dir_fd=lifetime.parent_fd)
        L._private_file(os.fstat(fd))
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining=deadline-time.monotonic()
                if remaining <= 0:
                    raise W.ReaderWindowBusy('reader_publish_busy') from exc
                time.sleep(min(0.005,remaining))
        W._named(install_id,lifetime.parent,lifetime.parent_fd,lifetime.parent_info,
                 'publish-cut.lock',fd)
        R._need(W._manifest(install_id,lifetime.parent,lifetime.parent_fd,lifetime.parent_info)
                == manifest, 'reader_windows_changed')
        _bound(endpoint)
        R._need(time.monotonic() < deadline, 'reader_publish_deadline')
        yield
        _bound(endpoint)
        R._need(W._manifest(install_id,lifetime.parent,lifetime.parent_fd,lifetime.parent_info)
                == manifest, 'reader_windows_changed')
    finally:
        if fd is not None:
            os.close(fd)
        lifetime.close()


def open_warm_component(endpoint, role, *, deadline):
    """Open one warm RO component with SH held until native close.

    This internal primitive does not attest native SQLite FD identity, writer
    cohort, semantic cross-store consistency, or a cold read capability.
    """
    W._deadline(deadline)
    R._need(type(role) is str and role in _FIELDS, 'reader_component_unsupported')
    _bound(endpoint)
    record=endpoint.record
    path=Path(record[_FIELDS[role]+'_path']);state=Path(record['state'])
    locks=ExitStack();owner=None
    try:
        locks.enter_context(W.reader_proof_windows(record['install_id'],deadline=deadline,roles=(role,)))
        _bound(endpoint)
        before,auxiliary=_files(path,state)
        expected=record.get(_FIELDS[role]+'_node')
        if expected is not None:
            R._need(R.persistent_node_equal(expected, before), 'reader_component_changed')
        R._need(auxiliary['-journal'] is None, 'reader_recovery_unproved')
        R._need(time.monotonic() < deadline, 'reader_open_deadline')
        from laya_store_primitives import _query_only_uri
        db=sqlite3.connect(_query_only_uri(path),uri=True,isolation_level=None,
                           timeout=0.2,factory=_ReaderSQLite)
        owner=_ReaderOwner(db,locks);_owners[id(db)]=owner
        with _validation_deadline((db,),deadline,reason='reader_open_deadline'):
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
            db.execute('SELECT count(*) FROM main.sqlite_master').fetchone()
        after, current_auxiliary = _files(path,state)
        R._need(after == before and current_auxiliary['-journal'] is None,
                'reader_component_changed')
        _bound(endpoint)
        R._need(time.monotonic() < deadline, 'reader_open_deadline')
        return owner
    except BaseException:
        if owner is not None:
            owner.close(deadline=time.monotonic()+2)
        else:
            locks.close()
        raise


@contextmanager
def pin_warm_components(endpoint, callbacks, *, deadline):
    """Pin four warm stores together without issuing a serving capability.

    The caller must still prove SQLite descriptor identity, cohort exclusion,
    cross-store semantics and the returned rows before using the snapshot.
    """
    W._deadline(deadline)
    _bound(endpoint)
    roles=('base','binding','catalog','sidecar')
    R._need(type(callbacks) is dict and set(callbacks)==set(roles)
            and all(callable(callbacks[role]) for role in roles),
            'reader_snapshot_input_invalid')

    @contextmanager
    def factory():
        with ExitStack() as stack:
            members=[]
            for role in roles:
                _bound(endpoint)
                path=Path(endpoint.record[_FIELDS[role]+'_path'])
                if role=='sidecar':
                    stack.enter_context(W.reader_proof_windows(endpoint.record['install_id'],
                                                               deadline=deadline,roles=(role,)))
                    if not os.path.lexists(path):
                        R._need(endpoint.record.get('sidecar_node') is None
                                and not any(os.path.lexists(str(path)+suffix)
                                            for suffix in ('-wal','-shm','-journal')),
                                'reader_component_changed')
                        members.append((path,None,None,None))
                        continue
                monitor=open_warm_component(endpoint,role,deadline=deadline)
                stack.callback(monitor.close)
                snapshot=open_warm_component(endpoint,role,deadline=deadline)
                stack.callback(snapshot.close)
                def pin(db, callback=callbacks[role]):
                    with _validation_deadline((db,),deadline):
                        return callback(db)
                members.append((path,monitor.db,snapshot.db,pin))
            yield members

    with L.pin_common(factory,deadline=deadline,
                           fallback=lambda: reader_publish_cut(endpoint,deadline=deadline)) as pinned:
        yield pinned


@dataclass(frozen=True)
class _WarmObservation:
    """Diagnostic snapshot, not an authorization or serving capability."""
    connections: tuple
    published: tuple
    sidecar: object
    generation_exclusion_epoch: int
    base_revision: object
    vector_seal: object


@contextmanager
def _validation_deadline(connections, deadline, *, reason='reader_snapshot_deadline'):
    """Interrupt SQLite work while proving a pinned image, then restore handlers."""
    with ExitStack() as stack:
        for db in connections:
            if db is not None:
                db._hold_progress_deadline(deadline)
                stack.callback(db._release_progress_deadline)
        try:
            yield
        except sqlite3.OperationalError as exc:
            if str(exc) == 'interrupted' and time.monotonic() >= deadline:
                raise R.journal.JournalEvidenceError(reason) from exc
            raise


@contextmanager
def inspect_warm_snapshot(endpoint, *, deadline):
    """Observe one route-bound four-store image without granting recall."""
    W._deadline(deadline)
    _bound(endpoint)
    record=endpoint.record
    with R.journal.read_only_journals():
        if endpoint in R._upgrade_readers:
            decision=R._recheck(endpoint)[2]['readerDecision']
        else:
            decision=R.journal.select_route(record['project'],action='recall')
        R._need(decision.get('state')=='candidate'
                and decision.get('projectRoot')==record['project']
                and decision.get('journalPath')==str(Path(record['state'])/R._JOURNAL)
                and decision.get('installId')==record['install_id']
                and decision.get('scopeId')==record['scope']
                and decision.get('dbIncarnation')==record['incarnation']
                and decision.get('dbPath')==record['db_path']
                and decision.get('vectorProfile',{}).get('profileId')==record['profile']
                and R._sha(R._raw(decision['recallSettings']))==record['settings']
                and R._freeze(decision['collections'])==record['roots'],
                'reader_selection_changed')
        callbacks={role:lambda db:db.execute('SELECT count(*) FROM main.sqlite_master').fetchone()[0]
                   for role in ('base','binding','catalog','sidecar')}
        with pin_warm_components(endpoint,callbacks,deadline=deadline) as pinned:
            base,binding,catalog,side=pinned.connections
            # This only observes a newer append-only exclusion set; a serving
            # reader must apply it before ranking and may not use stale pins.
            with _validation_deadline(pinned.connections,deadline):
                proof={}
                published=R._published_snapshot(base,decision,deadline=deadline,
                                                 allow_exclusion_advance=True,_proof=proof)
                base_revision=R._base_revision_snapshot(base,decision,published)
                epoch=base.execute('SELECT rev_epoch FROM generations WHERE scope=? AND gen=?',
                                   (decision['scopeId'],published[0])).fetchone()[0]
                R._capacity_snapshot(binding,catalog,Path(record['binding_path']),decision,published,
                                     deadline=deadline)
                observed_sidecar=None if side is None else R._sidecar_snapshot(side,decision,deadline=deadline)
            _bound(endpoint)
            current=(R._recheck(endpoint)[2]['readerDecision'] if endpoint in R._upgrade_readers else
                     R.journal.select_route(record['project'],action='recall'))
            R._need(current==decision,
                    'reader_selection_changed')
            R._need(time.monotonic() < deadline, 'reader_snapshot_deadline')
            with ExitStack() as guards:
                for db in pinned.connections:
                    if db is not None:
                        guard=db._hold_snapshot_transaction()
                        guards.callback(db._release_snapshot_transaction,guard)
                observation=_WarmObservation(pinned.connections,published,observed_sidecar,epoch,
                                             base_revision,proof['vector_seal'])
                context=_observations.set((*_observations.get(),(observation,endpoint,decision)))
                try:
                    yield observation
                finally:
                    _observations.reset(context)
            _bound(endpoint)
            R._need(time.monotonic() < deadline, 'reader_snapshot_deadline')
            current=(R._recheck(endpoint)[2]['readerDecision'] if endpoint in R._upgrade_readers else
                     R.journal.select_route(record['project'],action='recall'))
            R._need(current==decision,
                    'reader_selection_changed')


def drain_pending(*, deadline):
    W._deadline(deadline)
    error = None
    for owner in list(_owners.values()):
        if owner.pending and owner.pid == os.getpid() and owner.thread is threading.current_thread():
            try:
                R._need(time.monotonic() < deadline, 'reader_close_deadline')
                if owner.db.in_transaction:
                    owner.db.rollback()
                owner.close(deadline=deadline)
            except BaseException as exc:
                if error is None:
                    error = exc
    if error is not None:
        raise error
    return sum(owner.pending and owner.pid == os.getpid() for owner in _owners.values())
