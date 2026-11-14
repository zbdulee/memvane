"""Writer admission under the sync owner and reader lifetime."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import threading
import time

from memvane.journal import install_journal as J
from memvane.journal import journal_reader as R
from memvane.journal import reader_windows as W
from memvane.runtime.api import validate_sync_owner
from memvane.journal.types import BuildWriter, SelectedWriter

# Keep abandoned admissions strongly owned. Only explicit creator exit can
# release their lifetime and queue pin; no generator/finalizer does cleanup.
_build_issued = {}
_selected_issued = {}


def _selected_record(writer):
    R._need(type(writer) is SelectedWriter and writer in _selected_issued,
            'normal_writer_not_issued')
    context = _selected_issued[writer]
    R._need(context.pid == os.getpid() and context.thread is threading.current_thread()
            and context.entered and not context.finished, 'normal_writer_expired')
    return context


class _SelectedWriterContext:
    def __init__(self, decision, queue_owner):
        self.decision, self.queue_owner = decision, queue_owner
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self.entered = self.finished = False
        self.stack = ExitStack()
        self.marked = False

    def __enter__(self):
        from memvane.store import api as store_api
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread()
                and not self.entered and not self.finished, 'normal_writer_reentered')
        try:
            validate_sync_owner(self.queue_owner, Path(self.decision['storePath']))
            self.stack.callback(self.queue_owner.pin().close)
            self.lifetime = W._Lifetime(self.decision['installId'])
            self.stack.callback(self.lifetime.close)
            self.proof = J.verify_selected_route_for_write(self.decision)
            row = J.inspect_journal(self.decision['journalPath'])
            record = row['reader']
            R._need(row['state'] == 'selected' and isinstance(record, dict),
                    'writer_selection_unavailable')
            state, project = Path(self.decision['stateRoot']), Path(self.decision['projectRoot'])
            evidence = R._schema3_store(state, project, self.proof, self.decision,
                                        record['db_path'], record['worktree_id'])
            R._need(R.records_agree(record, evidence, tuple(evidence)), 'writer_plan_store_changed')
            meta = R._store_header(Path(record['db_path']), state, recover=True)
            R._need(meta is not None, 'writer_plan_store_changed')
            repository = json.loads(meta['repository_json'])
            binding = store_api.observe_binding(record['db_path'], repository=repository,
                                                scope_id=record['scope'], worktree_id=record['worktree_id'])
            R._need(binding['incarnation'] == record['incarnation']
                    and binding['policy_digest'] == record['settings_policy_digest']
                    and binding['saved']['binding_digest'] == record['worktree_binding'],
                    'writer_plan_store_changed')
            self.store = {'path': record['db_path'], 'incarnation': binding['incarnation'],
                          'worktree_id': record['worktree_id'], 'repository': repository,
                          'worktree_binding': record['worktree_binding'],
                          'policy_digest': binding['policy_digest']}
            self.entered = True
            token = object.__new__(SelectedWriter)
            self.writer = token
            _selected_issued[token] = self
            validate_normal_writer(token)
            return token
        except BaseException:
            self.finished = True
            if hasattr(self, 'writer'):
                _selected_issued.pop(self.writer, None)
            self.stack.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        R._need(self.pid == os.getpid() and self.thread is threading.current_thread(),
                'normal_writer_owner_changed')
        self.finished = True
        _selected_issued.pop(self.writer, None)
        self.stack.close()
        return False


def selected_v1_writer_scope(decision, *, queue_owner):
    return _SelectedWriterContext(decision, queue_owner)


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
        from memvane.journal import initial_build as build
        from memvane.journal.dispatch_target import dispatch_target
        from memvane.journal.install_journal import _attestation, _v1_provider_manifest, inspect_journal
        from memvane.store import api as store_api
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
            self.target = dispatch_target(self.decision['projectRoot'], build=True)
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
            meta = store_api.read_header(path, project_root=identity['projectRoot'])
            R._need(meta is not None and len(meta['worktrees']) == 1, 'writer_plan_store_changed')
            repository = json.loads(meta['repository_json'])
            worktree_id = meta['worktrees'][0]['worktree_id']
            binding = store_api.observe_binding(path, repository=repository, scope_id=identity['scopeId'],
                                                worktree_id=worktree_id)
            R._need(binding['incarnation'] == identity['storeIncarnation'], 'writer_plan_store_changed')
            self.store = {'path': str(path), 'incarnation': binding['incarnation'],
                          'worktree_id': worktree_id, 'repository': repository,
                          'worktree_binding': binding['saved']['binding_digest'],
                          'policy_digest': binding['policy_digest']}
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


def validate_normal_writer(writer, *, deadline=None):
    """Validate current admission metadata, not a continuing content snapshot."""
    if type(writer) is SelectedWriter:
        context = _selected_record(writer)
        if deadline is not None:
            W._deadline(deadline)
            R._need(time.monotonic() < deadline, 'normal_writer_deadline')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        context.lifetime.check()
        R._need(J.verify_selected_route_for_write(context.decision) == context.proof,
                'normal_writer_changed')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        return
    if type(writer) is BuildWriter:
        from memvane.journal import initial_build as build
        from memvane.journal.dispatch_target import dispatch_target
        context = _build_record(writer)
        if deadline is not None:
            W._deadline(deadline)
            R._need(time.monotonic() < deadline, 'normal_writer_deadline')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        context.lifetime.check()
        R._need(build.lookup(context.decision['projectRoot']) == context.record
                and dispatch_target(context.decision['projectRoot'], build=True) == context.target,
                'normal_writer_changed')
        validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
        return
    R._need(False, 'normal_writer_not_issued')


def writer_store(writer):
    """Validate the live writer and return its bound format 4 store."""
    validate_normal_writer(writer)
    if type(writer) is SelectedWriter:
        return json.loads(R._raw(_selected_record(writer).store))
    if type(writer) is BuildWriter:
        return json.loads(R._raw(_build_record(writer).store))
    R._need(False, 'normal_writer_not_issued')


def admission_proof(writer):
    """Return the owned admission proof after validating the live writer."""
    validate_normal_writer(writer)
    if type(writer) is SelectedWriter:
        return json.loads(R._raw(_selected_record(writer).proof))
    if type(writer) is BuildWriter:
        return json.loads(R._raw(_build_record(writer).proof))
    R._need(False, 'normal_writer_not_issued')


def mark_new_write(writer):
    """Commit first-write intent and reprove the selected writer."""
    if type(writer) is BuildWriter:
        return admission_proof(writer)
    if type(writer) is SelectedWriter:
        from contextlib import closing
        context = _selected_record(writer)
        validate_normal_writer(writer)
        if context.marked:
            return json.loads(R._raw(context.proof))
        path, db = J._open(context.decision['journalPath'], write=True)
        with closing(db):
            db.execute('BEGIN IMMEDIATE')
            try:
                row = J._inspect_v1_row(db)
                R._need(row['state'] == 'selected' and row['epoch'] == context.proof['selectionEpoch'],
                        'normal_writer_intent_premise_changed')
                validate_sync_owner(context.queue_owner, Path(context.decision['storePath']))
                context.lifetime.check()
                R._need(db.execute("UPDATE selection SET new_write_seen=1,epoch=epoch+1 "
                                   "WHERE id=1 AND state='selected' AND epoch=?", (row['epoch'],)).rowcount == 1,
                        'normal_writer_intent_premise_changed')
                J._inspect_v1_row(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        fresh = J.verify_selected_route_for_write(context.decision)
        stable = lambda value: {key: item for key, item in value.items() if key != 'selectionEpoch'}
        R._need(fresh['selectionEpoch'] == context.proof['selectionEpoch'] + 1
                and stable(fresh) == stable(context.proof),
                'normal_writer_intent_premise_changed')
        context.proof = fresh
        context.marked = True
        validate_normal_writer(writer)
        return json.loads(R._raw(fresh))
    R._need(False, 'normal_writer_not_issued')
