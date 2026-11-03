"""Normal base/queue admission, separate from reader and mover authority.

Admission proves one warm content image under EX. A later component opener
must revalidate its actual header under EX; this token is no immutable image
or all-writer cohort proof. Operational workers do not select this path yet.
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
from laya_index_sync import validate_sync_owner

# Keep abandoned admissions strongly owned. Only explicit creator exit can
# release their lifetime and queue pin; no generator/finalizer does cleanup.
_issued = {}


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


def validate_normal_writer(writer):
    """Validate current admission metadata, not a continuing content snapshot."""
    record = _record(writer)
    context = record['context']
    R._need(time.monotonic() < context.deadline, 'normal_writer_deadline')
    validate_sync_owner(context.queue_owner, context.plan.queue_path)
    P.validate_writer_plan(context.plan)
    context.lifetime.check()
    validate_sync_owner(context.queue_owner, context.plan.queue_path)
    R._need(time.monotonic() < context.deadline, 'normal_writer_deadline')


class _Endpoint:
    def __init__(self, writer, snapshot):
        self.writer = writer
        self.record = dict(snapshot['endpoint'])
        # Preferred reader B can differ from the canonical writer A.
        for role, key in (('base', 'db'), ('catalog', 'catalog'),
                          ('binding', 'binding'), ('sidecar', 'sidecar')):
            component = snapshot['components'][role]
            self.record[key+'_path'] = component['path']
            self.record[key+'_node'] = component['node']


def _owner(writer):
    validate_normal_writer(writer)
    context = _record(writer)['context']
    return context.endpoint, context


def _connection_check(writer, role, path):
    validate_normal_writer(writer)
    record = _record(writer)
    R._need(role in ('base', 'queue'), 'normal_writer_component_unsupported')
    snapshot = record['snapshot']
    row = snapshot['components'][role]
    R._need(Path(path) == Path(row['path']), 'normal_writer_component_changed')
    from laya_writer_proof_reader import _files
    files = _files(Path(path), Path(snapshot['endpoint']['state']))
    R._need(list(files[''][:2]) == row['node'], 'normal_writer_component_changed')
    if files['-wal'] is None or files['-shm'] is None:
        raise W.ReaderWindowBusy('normal_writer_cold_open_unproved')
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
        info = Path(str(path)+suffix).lstat()
        result.append((info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
    return tuple(result)


def _validate_identity(writer, role, db):
    """Actual fixed header/identity proof; no caller callback can omit it."""
    validate_normal_writer(writer)
    db.execute('BEGIN')
    try:
        if role == 'base':
            from laya_index_a import LayaIndexA
            LayaIndexA._check_header(db)
            incarnation = db.execute("SELECT value FROM schema_meta WHERE key='incarnation'").fetchone()[0]
        else:
            R._need(role == 'queue', 'normal_writer_component_unsupported')
            from laya_index_claim_queue import ClaimQueue
            incarnation = ClaimQueue._check_database(db)
        R._need(incarnation == writer.incarnation, 'normal_writer_incarnation_changed')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _approved(proof, snapshot):
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
    def __init__(self, plan, queue_owner, deadline):
        self.plan, self.queue_owner, self.deadline = plan, queue_owner, deadline
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.started = self.entered = self.finished = False
        self.stack = self.lifetime = self.writer = None

    def _creator(self):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'normal_writer_owner_changed')

    def __enter__(self):
        self._creator()
        R._need(not self.started and not self.finished, 'normal_writer_reentered')
        self.started = True
        W._deadline(self.deadline)
        R._need(time.monotonic() < self.deadline, 'normal_writer_deadline')
        P.validate_writer_plan(self.plan)
        validate_sync_owner(self.queue_owner, self.plan.queue_path)
        self.stack = ExitStack()
        try:
            pin = self.queue_owner.pin()
            self.stack.callback(pin.close)
            self.lifetime = W._Lifetime(self.plan.install_id)
            self.stack.callback(self.lifetime.close)
            with P.writer_proof_windows(self.plan, queue_owner=self.queue_owner,
                                        deadline=self.deadline) as window:
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
                self.project_node = snapshot['endpoint']['project_node']
                self.state_node = snapshot['endpoint']['state_node']
                self.endpoint = _Endpoint(writer, snapshot)
                self.entered = True
                _issued[writer] = dict(context=self, snapshot=snapshot, proof=proof)
            validate_normal_writer(writer)
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


def _prove_plan(context, plan):
    with P.writer_proof_windows(plan, queue_owner=context.queue_owner,
                                deadline=context.deadline) as window:
        with J.writer_identity_reads(window):
            snapshot = P._plan_record(plan)[1]
            proof = J.verify_selected_route_for_write(snapshot['decision'])
        P.validate_writer_proof_window(window)
        _approved(proof, snapshot)
    return proof


def mark_new_write(writer):
    """Commit first-write intent and reprove the same normal writer admission.

    A committed mark is forward-only. Reproof failure revokes this token;
    existing native handles retain cleanup-only ownership and their pins.
    """
    validate_normal_writer(writer)
    record = _record(writer)
    if record.get('marked', False):
        return json.loads(R._raw(record['proof']))
    context, old = record['context'], record['snapshot']
    _idle_connections(context)
    record['transitioning'] = True
    try:
        original_plan = context.plan
        _prove_plan(context, original_plan)
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
            R._need(time.monotonic() < context.deadline, 'normal_writer_deadline')
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
            R._need(time.monotonic() < context.deadline, 'normal_writer_deadline')

        R.mutate_image(path, mark, before_replace=before_replace)
        plan = P.capture_writer_plan(old['project'], executing_root=old['executing_root'])
        current = P._plan_record(plan)[1]
        R._need(current['bundle']['epoch'] == old['bundle']['epoch'] + 1
                and current['bundle']['new_write_seen'] == 1,
                'normal_writer_intent_unconfirmed')
        normalized = json.loads(R._raw(current))
        for key in ('epoch', 'new_write_seen'):
            normalized['bundle'][key] = old['bundle'][key]
        R._need(R._raw(normalized) == R._raw(old), 'normal_writer_intent_premise_changed')
        proof = _prove_plan(context, plan)
        # No current writer is public while either proof/close is incomplete.
        context.plan = plan
        record['snapshot'], record['proof'] = current, proof
        record['marked'] = True
        record['transitioning'] = False
        validate_normal_writer(writer)
        return json.loads(R._raw(proof))
    except BaseException:
        _issued.pop(writer, None)
        raise
