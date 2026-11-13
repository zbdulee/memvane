"""Normal base/queue admission, separate from reader and mover authority.

Admission proves one warm content image under EX. A later component opener
must revalidate its actual header under EX; this token is no immutable image
or all-writer cohort proof. Selected v5 sync workers use this scope.

A schema3 plan (``snapshot['endpoint']['store_format'] == 'schema3'``) issues
the same admission, lock windows and ``mark_new_write`` fence, but no v6
component: ``component_for`` and every v6 connection check refuse it with
``writer_selection_unavailable``, so a v6-only writer path stops before any
v6 open. ``writer_store`` returns the store the writer was admitted for.
"""
from contextlib import ExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time

import laya_install_journal as J
import laya_journal_reader as R
import laya_reader_windows as W
import laya_writer_open_plan as P
from sync_owner_lock import validate_sync_owner

# Keep abandoned admissions strongly owned. Only explicit creator exit can
# release their lifetime and queue pin; no generator/finalizer does cleanup.
_issued = {}
_build_issued = {}


@dataclass(frozen=True, eq=False, init=False)
class NormalWriter:
    install_id: str
    queue_path: Path
    scope_id: str
    incarnation: str

    def __init__(self):
        raise ValueError('normal_writer_not_issued')

    def __reduce__(self):
        raise TypeError('normal_writer_not_serializable')


class BuildWriter:
    """A same-thread initial-build writer held under the existing owners."""
    __slots__ = ()

    def __new__(cls):
        raise ValueError('normal_writer_not_issued')

    def __reduce__(self):
        raise TypeError('normal_writer_not_serializable')


def _build_record(writer):
    R._need(type(writer) is BuildWriter and writer in _build_issued,
            'normal_writer_not_issued')
    context = _build_issued[writer]
    R._need(context.pid == os.getpid() and context.thread is threading.current_thread()
            and context.entered and not context.finished, 'normal_writer_expired')
    return context


class _BuildWriterContext:
    def __init__(self, decision, queue_owner):
        self.decision, self.queue_owner = decision, queue_owner
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.entered = self.finished = False
        self.stack = ExitStack()

    def __enter__(self):
        import laya_initial_build as build
        import laya_runtime_dispatch as dispatch
        from laya_install_journal import _attestation, _v1_provider_manifest, inspect_journal
        from laya_view_store import ViewStore
        from laya_store_primitives import _query_only_uri
        import sqlite3
        from contextlib import closing
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread()
                and not self.entered and not self.finished, 'normal_writer_reentered')
        state = Path(self.decision['stateRoot'])
        try:
            validate_sync_owner(self.queue_owner, Path(self.decision['storePath']))
            self.stack.callback(self.queue_owner.pin().close)
            if not self.stack.enter_context(build.owner(state)):
                raise ValueError('initial_build_busy')
            self.lifetime = W._Lifetime(self.decision['installId'])
            self.stack.callback(self.lifetime.close)
            self.target = dispatch.resolve('laya-sync', self.decision['projectRoot'])
            self.record = build.lookup(self.decision['projectRoot'])
            R._need(self.record is not None and self.record['phase'] in ('registered', 'building'),
                    'initial_build_unavailable')
            identity = self.record['identity']
            row = inspect_journal(identity['journalPath'])
            attestation, digest = _attestation(Path(identity['journalPath']))
            R._need(digest == identity['attestationSha256'], 'worker_attestation_changed')
            manifest = _v1_provider_manifest(attestation, row['vectorProfile'])
            self.proof = {'productRouteAllowed': True, 'buildPending': True,
                          'journalPath': identity['journalPath'], 'selectionEpoch': row['epoch'],
                          'vectorProfile': row['vectorProfile'], 'providerManifest': manifest}
            path = Path(identity['storePath'])
            with closing(sqlite3.connect(_query_only_uri(path), uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                meta = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
                rows = db.execute('SELECT worktree_id FROM worktrees WHERE project_root=?',
                                  (identity['projectRoot'],)).fetchall()
            R._need(meta is not None and len(rows) == 1, 'writer_plan_store_changed')
            repository = json.loads(meta[0])
            store = ViewStore.open(path, repository=repository, scope_id=identity['scopeId'])
            try:
                saved, _capture = store._observe_worktree(rows[0][0])
                R._need(store._incarnation == identity['storeIncarnation'], 'writer_plan_store_changed')
                self.store = {'path': str(path), 'incarnation': store._incarnation,
                              'worktree_id': rows[0][0], 'repository': repository,
                              'worktree_binding': saved['binding_digest'], 'policy_digest': store._policy}
            finally:
                store.close()
            self.entered = True
            token = object.__new__(BuildWriter)
            self.writer = token
            _build_issued[token] = self
            validate_normal_writer(token)
            return token
        except BaseException:
            self.finished = True
            if hasattr(self, 'writer'):
                _build_issued.pop(self.writer, None)
            self.stack.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'normal_writer_owner_changed')
        self.finished = True
        _build_issued.pop(self.writer, None)
        self.stack.close()
        return False


def build_writer_scope(decision, *, queue_owner):
    return _BuildWriterContext(decision, queue_owner)


def _record(writer):
    R._need(type(writer) is NormalWriter and writer in _issued, 'normal_writer_not_issued')
    record = _issued[writer]
    context, snapshot = record['context'], record['snapshot']
    context._creator()
    R._need(not context.finished and context.entered, 'normal_writer_expired')
    R._need(not record.get('transitioning', False), 'normal_writer_transitioning')
    decision = snapshot['decision']
    R._need(writer.install_id == decision['installId']
            and writer.queue_path == Path(snapshot['components']['queue']['path'])
            and writer.scope_id == decision['scopeId']
            and writer.incarnation == decision['dbIncarnation'], 'normal_writer_changed')
    return record


def validate_normal_writer(writer, *, deadline=None):
    """Validate current admission metadata, not a continuing content snapshot."""
    if type(writer) is BuildWriter:
        import laya_initial_build as build
        import laya_runtime_dispatch as dispatch
        context = _build_record(writer)
        if deadline is not None:
            W._deadline(deadline)
            R._need(time.monotonic() < deadline, 'normal_writer_deadline')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        context.lifetime.check()
        R._need(build.lookup(context.decision['projectRoot']) == context.record
                and dispatch.resolve('laya-sync', context.decision['projectRoot']) == context.target,
                'normal_writer_changed')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        return
    record = _record(writer)
    context = record['context']
    if deadline is None:
        deadline = context.operation_deadline()
    else:
        W._deadline(deadline)
        deadline = min(deadline, context.operation_deadline())
    context.check_deadline(deadline)
    validate_sync_owner(context.queue_owner, context.plan.queue_path)
    P.validate_writer_plan(context.plan)
    context.lifetime.check()
    validate_sync_owner(context.queue_owner, context.plan.queue_path)
    context.check_deadline(deadline)


def _require_v6(snapshot):
    R._need(snapshot['endpoint']['store_format'] == 'v6', 'writer_selection_unavailable')


class _Endpoint:
    def __init__(self, writer, snapshot):
        self.writer = writer
        self.record = dict(snapshot['endpoint'])
        if self.record['store_format'] == 'schema3':
            # db_path/db_node already name the schema3 store; there is no
            # v6 role to point at (component_for refuses schema3 writers).
            return
        # Preferred reader B can differ from the canonical writer A.
        for role, key in (('base', 'db'), ('catalog', 'catalog'),
                          ('binding', 'binding'), ('sidecar', 'sidecar')):
            component = snapshot['components'][role]
            self.record[key+'_path'] = component['path']
            self.record[key+'_node'] = component['node']


def _owner(writer):
    validate_normal_writer(writer)
    record = _record(writer)
    _require_v6(record['snapshot'])
    context = record['context']
    return context.endpoint, context


def writer_store(writer):
    """Validate the live writer; return its schema3 store, or None for v6.

    Same keys as ``laya_writer_open_plan.plan_store``.
    """
    validate_normal_writer(writer)
    if type(writer) is BuildWriter:
        return json.loads(R._raw(_build_record(writer).store))
    store = _record(writer)['snapshot'].get('store')
    return None if store is None else json.loads(R._raw(store))


def _connection_check(writer, role, path):
    validate_normal_writer(writer)
    record = _record(writer)
    R._need(role in ('base', 'queue', 'catalog', 'binding', 'sidecar'),
            'normal_writer_component_unsupported')
    snapshot = record['snapshot']
    _require_v6(snapshot)
    row = snapshot['components'][role]
    R._need(Path(path) == Path(row['path']), 'normal_writer_component_changed')
    from laya_writer_proof_reader import _files
    files = _files(Path(path), Path(snapshot['endpoint']['state']))
    R._need(list(files[''][:2]) == row['node'], 'normal_writer_component_changed')
    R._need(files['-journal'] is None, 'normal_writer_recovery_unproved')
    return files


def _connection_pin(writer, role, path):
    before = _connection_check(writer, role, path)
    pin = _record(writer)['context'].queue_owner.pin()
    try:
        R._need(_connection_check(writer, role, path) == before,
                'normal_writer_component_changed')
        return pin
    except BaseException:
        pin.close()
        raise


def _connection_image(writer, role, path):
    """Detect ordinary commits across RO/RW admission; no immutable seal."""
    _connection_check(writer, role, path)
    # Read-only SQLite can update existing SHM, so only the main/WAL content
    # files contribute revision metadata. Fixed identities are checked again
    # on the RW connection; timestamps are not a same-UID tampering boundary.
    result = []
    for suffix in ('', '-wal'):
        try:
            info = Path(str(path)+suffix).lstat()
        except FileNotFoundError:
            if suffix == '-wal':
                result.append(None)
                continue
            raise
        if suffix == '-wal' and info.st_size == 0:
            result.append(None)
            continue
        result.append((info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
    return tuple(result)


def _validate_identity(writer, role, db):
    """The retired component windows cannot authorize a v1 store write."""
    validate_normal_writer(writer)
    R._need(False, 'normal_writer_component_unsupported')


def _approved(proof, snapshot):
    from memvane_version import same_major, writer_floor_allows
    try:
        matching_major = same_major(proof['stateFormat'])
    except (OSError, KeyError, TypeError, ValueError):
        R._need(False, 'writer_version_invalid')
    R._need(matching_major, 'state_format_mismatch')
    try:
        writable = writer_floor_allows(proof['writerFloor'])
    except (OSError, KeyError, TypeError, ValueError):
        R._need(False, 'writer_version_invalid')
    R._need(writable, 'writer_floor')
    decision, selected = snapshot['decision'], snapshot['bundle']['selected']
    expected = dict(status='installer_active_full_plugin_verified',
        executingPluginRoot=snapshot['executing_root'], pluginRoot=snapshot['executing_root'],
        journalPath=decision['journalPath'], selectionEpoch=snapshot['bundle']['epoch'],
        installId=decision['installId'], scopeId=decision['scopeId'],
        dbIncarnation=decision['dbIncarnation'], projectRoot=snapshot['project'],
        generation=selected['generation'], sourceCommit=selected['sourceCommit'],
        bundleManifestSha256=selected['bundleManifestSha256'],
        ownershipManifestSha256=selected['ownershipManifestSha256'],
        vectorProfile=selected['vectorProfile'])
    R._need(type(proof) is dict and proof.get('productRouteAllowed') is True
            and type(proof.get('selectionEpoch')) is int
            and type(proof.get('providerManifest')) is dict
            and all(proof.get(key) == value for key, value in expected.items()),
            'normal_writer_full_proof_required')


class _NormalWriterContext:
    def __init__(self, plan, queue_owner, deadline, operation_timeout=None):
        self.plan, self.queue_owner, self.deadline = plan, queue_owner, deadline
        self.operation_timeout = operation_timeout
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.started = self.entered = self.finished = False
        self.stack = self.lifetime = self.writer = None

    def _creator(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'normal_writer_owner_changed')

    def check_deadline(self, deadline=None):
        limit = self.deadline if deadline is None else deadline
        if limit is not None:
            R._need(time.monotonic() < limit, 'normal_writer_deadline')

    def operation_deadline(self):
        if self.operation_timeout is None:
            return self.deadline
        return time.monotonic() + self.operation_timeout

    def __enter__(self):
        self._creator()
        R._need(not self.started and not self.finished, 'normal_writer_reentered')
        self.started = True
        deadline = self.operation_deadline()
        W._deadline(deadline)
        self.check_deadline(deadline)
        P.validate_writer_plan(self.plan)
        validate_sync_owner(self.queue_owner, self.plan.queue_path)
        self.stack = ExitStack()
        try:
            pin = self.queue_owner.pin()
            self.stack.callback(pin.close)
            self.lifetime = W._Lifetime(self.plan.install_id)
            self.stack.callback(self.lifetime.close)
            with P.writer_proof_windows(self.plan, queue_owner=self.queue_owner,
                                        deadline=deadline) as window:
                with J.writer_identity_reads(window):
                    snapshot = P._plan_record(self.plan)[1]
                    proof = J.verify_selected_route_for_write(snapshot['decision'])
                # Both native RO readers are closed while every physical EX
                # is still held. Complete approval before releasing that EX.
                P.validate_writer_proof_window(window)
                _approved(proof, snapshot)
                writer = object.__new__(NormalWriter)
                object.__setattr__(writer, 'install_id', self.plan.install_id)
                object.__setattr__(writer, 'queue_path', self.plan.queue_path)
                object.__setattr__(writer, 'scope_id', snapshot['decision']['scopeId'])
                object.__setattr__(writer, 'incarnation', snapshot['decision']['dbIncarnation'])
                self.writer = writer
                self.project, self.state = Path(snapshot['project']), Path(snapshot['endpoint']['state'])
                # The plan proves the persisted nodes; retain live nodes for
                # this process's subsequent owner/race checks.
                self.project_node = R._node(self.project)
                self.state_node = R._node(self.state)
                self.endpoint = _Endpoint(writer, snapshot)
                self.entered = True
                _issued[writer] = dict(context=self, snapshot=snapshot, proof=proof)
            validate_normal_writer(writer)
            self.check_deadline(deadline)
            return writer
        except BaseException:
            if self.writer is not None:
                _issued.pop(self.writer, None)
            self.finished = True
            self.stack.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._creator()
        if self.finished or not self.entered:
            return False
        # Revocation precedes lock release and needs no current write proof.
        self.finished = True
        _issued.pop(self.writer, None)
        self.stack.close()
        return False


def normal_writer_scope(plan, *, queue_owner, deadline):
    """Issue base/queue admission after an owned, approved full write proof."""
    return _NormalWriterContext(plan, queue_owner, deadline)


def worker_writer_scope(plan, *, queue_owner, operation_timeout):
    """Keep ownership for the worker lifetime; bound each proof operation."""
    W._deadline(operation_timeout)
    R._need(0 < operation_timeout <= 30, 'normal_writer_timeout_invalid')
    return _NormalWriterContext(plan, queue_owner, None, operation_timeout)


def admission_proof(writer):
    """Return the owned admission proof after validating the live writer."""
    validate_normal_writer(writer)
    if type(writer) is BuildWriter:
        return json.loads(R._raw(_build_record(writer).proof))
    return json.loads(R._raw(_record(writer)['proof']))






def _idle_connections(context):
    for owner in list(W._owned.values()):
        if W._component(owner.component)[2] is not context:
            continue
        owner.check()
        R._need(owner.db is not None and not owner.pending and owner.transaction is None
                and not owner.db.in_transaction, 'normal_writer_snapshot_busy')
        R._need(all(getattr(cursor, '_window_closed', False) is True
                    or getattr(cursor, '_window_exhausted', False) is True
                    for cursor in list(owner.db._live_cursors.values())),
                'normal_writer_snapshot_busy')


def _prove_plan(context, plan, deadline, timings=None, stage=None):
    """``timings[stage + '_lock']``/``[stage + '_sql']``: ms waiting for the
    proof windows, then inside them (kept when the proof fails)."""
    started = time.monotonic()
    entered = None
    try:
        with P.writer_proof_windows(plan, queue_owner=context.queue_owner,
                                    deadline=deadline) as window:
            entered = time.monotonic()
            with J.writer_identity_reads(window):
                snapshot = P._plan_record(plan)[1]
                proof = J.verify_selected_route_for_write(snapshot['decision'])
            P.validate_writer_proof_window(window)
            _approved(proof, snapshot)
        return proof
    finally:
        if timings is not None:
            ended = time.monotonic()
            opened = ended if entered is None else entered
            timings[stage + '_lock'] = _ms(opened - started)
            timings[stage + '_sql'] = _ms(ended - opened)


def _ms(seconds):
    return max(0, int(round(seconds * 1000)))


def mark_new_write(writer):
    """Commit first-write intent and reprove the same normal writer admission.

    A committed mark is forward-only. Reproof failure revokes this token;
    existing native handles retain cleanup-only ownership and their pins.
    Each stage (validation and first proof, the image CAS, the reproof) gets
    its own operation deadline, so two proofs do not share one budget. A
    failure carries ``mark_timings`` (ms per stage; ``p1``/``p2`` split into
    proof-window wait and the reads inside).
    """
    if type(writer) is BuildWriter:
        return admission_proof(writer)
    record = _record(writer)
    context = record['context']
    timings, image_started = {}, None
    deadline = context.operation_deadline()
    validate_normal_writer(writer, deadline=deadline)
    if record.get('marked', False):
        context.check_deadline(deadline)
        return json.loads(R._raw(record['proof']))
    old = record['snapshot']
    _idle_connections(context)
    record['transitioning'] = True
    try:
        original_plan = context.plan
        _prove_plan(context, original_plan, deadline, timings, 'p1')
        deadline = context.operation_deadline()
        image_started = time.monotonic()
        path = Path(old['decision']['journalPath'])

        def mark(image):
            # The reader CAS has its own epoch. Check its complete selected
            # premise inside the same image-writer lock as the bundle mark.
            row = J._row(image)
            selection, endpoint = R._selection(image)
            R._need(J._canon(row) == J._canon(old['bundle'])
                    and selection.epoch == old['reader_epoch']
                    and selection.preferred_endpoint == old['endpoint_id']
                    and R._raw(endpoint) == R._raw(old['endpoint']),
                    'normal_writer_intent_premise_changed')
            R._need(row['state'] == 'active' and row['selected'] is not None,
                    'normal_writer_intent_unavailable')
            validate_sync_owner(context.queue_owner, context.plan.queue_path)
            context.check_deadline(deadline)
            return J._mutate_row(image, path, row, old['bundle']['epoch'],
                                 lambda _path, _row: {'new_write_seen': 1})

        def before_replace():
            context._creator()
            R._need(_issued.get(writer) is record and record.get('transitioning') is True
                    and not context.finished and context.entered
                    and context.plan is original_plan, 'normal_writer_expired')
            _idle_connections(context)
            validate_sync_owner(context.queue_owner, original_plan.queue_path)
            P.validate_writer_plan(original_plan)
            context.lifetime.check()
            validate_sync_owner(context.queue_owner, original_plan.queue_path)
            context.check_deadline(deadline)

        R.mutate_image(path, mark, before_replace=before_replace)
        context.check_deadline(deadline)
        plan = P.capture_writer_plan(old['project'], executing_root=old['executing_root'])
        current = P._plan_record(plan)[1]
        R._need(current['bundle']['epoch'] == old['bundle']['epoch'] + 1
                and current['bundle']['new_write_seen'] == 1,
                'normal_writer_intent_unconfirmed')
        normalized = json.loads(R._raw(current))
        for key in ('epoch', 'new_write_seen'):
            normalized['bundle'][key] = old['bundle'][key]
        R._need(R._raw(normalized) == R._raw(old), 'normal_writer_intent_premise_changed')
        # The recapture belongs to the image stage: check its deadline before
        # the reproof takes a fresh one.
        context.check_deadline(deadline)
        timings['img'] = _ms(time.monotonic() - image_started)
        deadline = context.operation_deadline()
        proof = _prove_plan(context, plan, deadline, timings, 'p2')
        # No current writer is public while either proof/close is incomplete.
        context.plan = plan
        record['snapshot'], record['proof'] = current, proof
        record['marked'] = True
        record['transitioning'] = False
        validate_normal_writer(writer, deadline=deadline)
        context.check_deadline(deadline)
        return json.loads(R._raw(proof))
    except BaseException as exc:
        _issued.pop(writer, None)
        if image_started is not None and 'img' not in timings:
            timings['img'] = _ms(time.monotonic() - image_started)
        try:
            exc.mark_timings = dict(timings)
        except AttributeError:
            pass
        raise
