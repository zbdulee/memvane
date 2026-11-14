"""Selected Laya wiki compile boundary, shared by enqueue, writer and verifier.

The installer journal authorizes the route. A project setting or an environment
variable alone never authorizes a selected write or a fallback to legacy QMD.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
import json
from memvane.kernel.resource_deferred import ResourceDeferred

_compile_owner = ContextVar('selected_compile_owner', default=None)


@contextmanager
def _owner_scope(bound, owner):
    token=_compile_owner.set((bound,owner))
    try:
        yield
    finally:
        _compile_owner.reset(token)


def _compile_proof(decision, *, owner=None):
    from memvane.settings import api as project_dir
    from memvane.journal.api import verify_selected_route_for_write
    active=_compile_owner.get()
    if owner is None and active is not None and active[0][0]==decision:
        owner=active[1]
    if owner is not None:
        from memvane.journal.api import owned_selected_proof as _owned_selected_proof
        return _owned_selected_proof(decision,owner)
    if 'journalPath' not in decision:
        return verify_selected_route_for_write(decision,'compile')
    from memvane.journal import api as journal
    from memvane.runtime.api import sync_owner
    from memvane.kernel.resource_deferred import ResourceDeferred
    with sync_owner(Path(decision['storePath'])) as acquired:
        if acquired is None:
            raise ResourceDeferred('owner_busy')
        with journal.read_only_journals():
            return verify_selected_route_for_write(decision,'compile')


def selected(root: Path):
    from memvane.journal.api import select_route

    root = Path(root).resolve()
    decision = select_route(str(root), action='compile')
    if decision.get('state') == 'legacy':
        return None
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(root):
        raise ValueError('selected_compile_route_unavailable')
    proof = _compile_proof(decision)
    if proof.get('productRouteAllowed') is not True:
        raise ValueError('selected_compile_journal_unavailable')
    collections = decision.get('collections')
    if (not isinstance(collections, list) or len(collections) != 2 or
            sorted(item.get('role') for item in collections if isinstance(item, dict))
            != ['raw', 'wiki']):
        raise ValueError('selected_compile_collections_unavailable')
    return decision, proof


def recheck(bound, *, owner=None):
    if bound is None:
        return
    active=_compile_owner.get()
    if owner is None and active is not None and active[0]==bound:
        owner=active[1]
    proof = _compile_proof(bound[0],owner=owner)
    if proof != bound[1]:
        raise ValueError('selected_compile_proof_changed')


def route_identity(bound):
    if bound is None:
        return None
    decision = bound[0]
    return {key: decision[key] for key in ('installId', 'scopeId', 'storeIncarnation')}


def schema3_checkout(bound):
    """Capture the worktree registered by the selected schema3 endpoint."""
    if bound is None:
        return None
    decision = bound[0]
    from memvane.journal import api as journal
    from memvane.journal.types import JournalEvidenceError
    path = decision.get('journalPath')
    if not isinstance(path, str) or journal.journal_version(path) != 1:
        return None
    with journal.read_only_journals():
        journal_path, endpoint, epoch, record = journal.selected_journal_reader(path)
        if record.get('store_format') != 'schema3':
            return None
        recheck(bound)
        row = journal.schema3_reader_repository(decision, record, journal_path)
        from memvane.store.api import observe_binding
        binding = observe_binding(record['db_path'], repository=json.loads(row[0]), scope_id=record['scope'],
                                  worktree_id=record['worktree_id'], reader=True)
        if not binding['saved']['binding_digest'] == record['worktree_binding']:
            raise JournalEvidenceError('reader_store_changed')
        repository = binding['repository']
        worktree = binding['record']
        issued = _Schema3Checkout(decision, endpoint, epoch, repository, worktree)
        if not issued._selection_current():
            raise ValueError('reader_selection_changed')
        return issued


class _Schema3Checkout:
    def __init__(self, decision, endpoint, epoch, repository, worktree):
        self.decision = decision
        self.endpoint = endpoint
        self.epoch = epoch
        self.repository = repository
        self.worktree = worktree

    def _selection_current(self):
        from memvane.journal.api import schema3_selection_current
        return schema3_selection_current(self.decision, self.endpoint, self.epoch)

    def capture(self):
        from memvane.store import api as checkout
        if not self._selection_current():
            raise ValueError('reader_selection_changed')
        capture = checkout.capture_view(self.decision['projectRoot'], self.repository,
                                        self.worktree, fresh=True)
        if not self._selection_current():
            raise ValueError('reader_selection_changed')
        return capture

    def matches(self, capture):
        from memvane.store import api as checkout
        return (self._selection_current()
                and checkout.capture_matches(capture, self.repository, self.worktree, fresh=True)
                and self._selection_current())


def request_compile(root: Path, bound, *, owner=None):
    if bound is None:
        return
    recheck(bound, owner=owner)
    from memvane.runtime.api import request
    result = request('laya-compile', str(Path(root).resolve()))
    if not isinstance(result, dict) or result.get('status') not in ('queued', 'busy'):
        raise ValueError('selected_compile_wake_enqueue_failed')


def request_sync(root: Path, bound=None):
    bound = selected(root) if bound is None else bound
    if bound is None:
        return False
    recheck(bound)
    from memvane.runtime.api import request
    result = request('laya-sync', str(Path(root).resolve()))
    if not isinstance(result, dict) or result.get('status') not in ('queued', 'busy'):
        raise ValueError('selected_wiki_sync_enqueue_failed')
    return True




from memvane.wiki.workspace.types import DEFERRED_REASONS as _DEFERRED_REASONS


def deferred_result(exc):
    """A bounded public result for a retryable compile resource refusal."""
    reason = str(exc)
    return {'state': 'deferred',
            'reason': reason if reason in _DEFERRED_REASONS else 'resource_deferred'}








def _queue_busy(queue: Path, *, receipt_writer=None):
    """True if ``queue`` holds rows or a live claim, decided under its lock.

    Dead-owner ``<queue>.claimed.*`` rows are requeued first. Claim presence and
    queue content are read under the same lock ``claim_queue`` moves rows
    under, so a concurrent claim is seen as either the queue or its claim.
    """
    if not queue.parent.is_dir():
        return False
    import fcntl
    from memvane.wiki.workspace.queue_paths import _queue_lock_path
    from memvane.wiki.workspace.queue import reclaim_orphaned_claimed
    prefix = queue.name + '.claimed.'
    with open(_queue_lock_path(queue), 'a', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            reclaim_orphaned_claimed(queue, receipt_writer=receipt_writer)
            if any(child.name.startswith(prefix) for child in queue.parent.iterdir()):
                return True
            return bool(queue.exists() and queue.stat().st_size)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
