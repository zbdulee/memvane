"""Selected Laya wiki compile boundary, shared by enqueue, writer and verifier.

The installer journal authorizes the route. A project setting or an environment
variable alone never authorizes a selected write or a fallback to legacy QMD.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import re
import subprocess
import sys
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
    from memvane.settings import project_dir
    from laya_install_journal import verify_selected_route_for_write
    active=_compile_owner.get()
    if owner is None and active is not None and active[0][0]==decision:
        owner=active[1]
    if owner is not None:
        from laya_product_sync_worker import _owned_selected_proof
        return _owned_selected_proof(decision,owner)
    if 'journalPath' not in decision:
        return verify_selected_route_for_write(decision,'compile')
    import laya_install_journal as journal
    from sync_owner_lock import sync_owner
    from memvane.kernel.resource_deferred import ResourceDeferred
    with sync_owner(Path(decision['storePath'])) as acquired:
        if acquired is None:
            raise ResourceDeferred('owner_busy')
        with journal.read_only_journals():
            if journal.journal_version(decision['journalPath'])==5:
                from laya_product_sync_worker import _owned_selected_proof
                return _owned_selected_proof(decision,acquired)
            with journal.legacy_identity_reads():
                return verify_selected_route_for_write(decision,'compile')


def selected(root: Path):
    from laya_index_host_route import select_route

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
    import laya_install_journal as journal
    import laya_journal_reader as reader
    path = decision.get('journalPath')
    if not isinstance(path, str) or journal.journal_version(path) != 5:
        return None
    with journal.read_only_journals():
        journal_path = reader._journal(path)
        with reader._readonly(journal_path, journal_path.parent) as db:
            selection, record = reader._selection(db)
        if record.get('store_format') != 'schema3':
            return None
        recheck(bound)
        reader._need(record['install_id'] == decision['installId']
                     and record['scope'] == decision['scopeId']
                     and record['project'] == decision['projectRoot']
                     and record['state'] == str(journal_path.parent), 'reader_install_changed')
        state = Path(record['state'])
        actual = reader._schema3_store(state, Path(record['project']),
                                       {'scopeId': record['scope']}, decision,
                                       record['db_path'], record['worktree_id'])
        for key in ('db_path', 'incarnation', 'repository_digest', 'worktree_id',
                    'worktree_binding', 'settings_policy_digest'):
            reader._need(actual[key] == record[key], 'reader_store_changed')
        reader._need(reader.persistent_node_equal(actual['db_node'], record['db_node']),
                     'reader_store_changed')
        with reader._readonly(Path(record['db_path']), state) as db:
            row = db.execute('SELECT repository_json FROM store_meta WHERE singleton=1').fetchone()
        reader._need(row is not None and reader._repository_digest(row[0]) == record['repository_digest'],
                     'reader_store_changed')
        from memvane.store.view_store import ViewStore
        store = ViewStore.open_reader(record['db_path'], repository=json.loads(row[0]), scope_id=record['scope'])
        try:
            saved, _capture = store._observe_worktree(record['worktree_id'])
            reader._need(saved['binding_digest'] == record['worktree_binding'], 'reader_store_changed')
            from memvane.store.blob_store import _worktree_record
            repository = store._repository
            worktree = _worktree_record(saved, repository, store._policy)
        finally:
            store.close()
        issued = _Schema3Checkout(decision, selection.preferred_endpoint, selection.epoch,
                                  repository, worktree)
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
        import laya_install_journal as journal
        import laya_journal_reader as reader
        from laya_index_host_route import select_route
        if select_route(self.decision['projectRoot'], action='compile') != self.decision:
            return False
        with journal.read_only_journals():
            path = reader._journal(self.decision['journalPath'])
            with reader._readonly(path, path.parent) as db:
                selection, record = reader._selection(db)
            if (selection.preferred_endpoint != self.endpoint or selection.epoch != self.epoch
                    or record.get('store_format') != 'schema3'):
                return False
            current = reader._schema3_store(Path(record['state']), Path(record['project']),
                                            {'scopeId': record['scope']}, self.decision,
                                            record['db_path'], record['worktree_id'])
            return (all(current[key] == record[key] for key in
                        ('db_path', 'incarnation', 'repository_digest', 'worktree_id',
                         'worktree_binding', 'settings_policy_digest'))
                    and reader.persistent_node_equal(current['db_node'], record['db_node']))

    def capture(self):
        from memvane.store import checkout_view as checkout
        if not self._selection_current():
            raise ValueError('reader_selection_changed')
        capture = checkout.capture_view(self.decision['projectRoot'], self.repository,
                                        self.worktree, fresh=True)
        if not self._selection_current():
            raise ValueError('reader_selection_changed')
        return capture

    def matches(self, capture):
        from memvane.store import checkout_view as checkout
        return (self._selection_current()
                and checkout.capture_matches(capture, self.repository, self.worktree, fresh=True)
                and self._selection_current())


def request_compile(root: Path, bound, *, owner=None):
    if bound is None:
        return
    recheck(bound, owner=owner)
    from runtime_work import request
    result = request('laya-compile', str(Path(root).resolve()))
    if not isinstance(result, dict) or result.get('status') not in ('queued', 'busy'):
        raise ValueError('selected_compile_wake_enqueue_failed')


def request_sync(root: Path, bound=None):
    bound = selected(root) if bound is None else bound
    if bound is None:
        return False
    recheck(bound)
    from runtime_work import request
    result = request('laya-sync', str(Path(root).resolve()))
    if not isinstance(result, dict) or result.get('status') not in ('queued', 'busy'):
        raise ValueError('selected_wiki_sync_enqueue_failed')
    return True


def wiki_matches(root: Path, wiki_root: Path, text: str, top_k: int,
                 cap_chars: int, *, bound=None):
    """Index candidates plus live unpublished cards, with bounded body reads."""
    bound = selected(root) if bound is None else bound
    if bound is None:
        raise ValueError('selected_compile_required')
    recheck(bound)
    approved = next(item['sourceRoot'] for item in bound[0]['collections']
                    if item['role'] == 'wiki')
    if str(wiki_root.resolve()) != approved:
        raise ValueError('selected_wiki_root_changed')
    if (type(top_k) is not int or not 1 <= top_k <= 20 or
            type(cap_chars) is not int or not 1 <= cap_chars <= 12000):
        raise ValueError('selected_wiki_retrieval_budget')
    import wiki_compile_retrieval
    matches = wiki_compile_retrieval.matches(root, wiki_root, text, top_k,
                                             cap_chars, bound)
    recheck(bound)
    return matches


_DEFERRED_REASONS = frozenset(('owner_busy', 'writer_proof_deadline',
                               'reader_lifetime_busy', 'writer_proof_physical_busy'))


def deferred_result(exc):
    """A bounded public result for a retryable compile resource refusal."""
    reason = str(exc)
    return {'state': 'deferred',
            'reason': reason if reason in _DEFERRED_REASONS else 'resource_deferred'}


def _worker_failure_code(stderr):
    """Report a fixed exception category, never child stderr or a path."""
    if not isinstance(stderr, str):
        return 'worker_error'
    last = stderr.rstrip().splitlines()[-1:]  # stderr can contain private text
    match = re.match(r'^(?:[A-Za-z_][A-Za-z0-9_]*\.)*(ValueError|RuntimeError|OSError|PermissionError|ResourceDeferred)(?::|$)', last[0]) if last else None
    return {'ValueError': 'worker_value_error', 'RuntimeError': 'worker_runtime_error',
            'OSError': 'worker_os_error', 'PermissionError': 'worker_permission_error',
            'ResourceDeferred': 'worker_resource_deferred'}.get(match.group(1), 'worker_error') if match else 'worker_error'


def run(project: Path):
    try:
        return _run(project)
    except ResourceDeferred as exc:
        return deferred_result(exc)


def _run(project: Path):
    """One bounded durable compile/verify pass; ACK only drained queues."""
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        return {'state': 'deferred', 'reason': 'selected_project_unavailable'}
    bound = selected(project)
    if bound is None or bound[0]['projectRoot'] != str(project):
        return {'state': 'deferred', 'reason': 'selected_compile_route_unavailable'}
    from wiki_compile_enqueue import _safe_queue_path
    from memvane.wiki.workspace import compile_paths as cp
    source = _safe_queue_path(project, cp.rel(cp.SOURCE_QUEUE))
    verify = _safe_queue_path(project, cp.rel(cp.VERIFY_QUEUE))
    if source is None or verify is None:
        return {'state': 'deferred', 'reason': 'unsafe_compile_queues'}
    import wiki_lifecycle_request
    try:
        lifecycle = wiki_lifecycle_request.claim(project)
    except (OSError, ValueError):
        lifecycle = None
    if lifecycle is not None:
        # Before the compile worker, so the rows it enqueues compile in this pass.
        wiki_lifecycle_request.run_lifecycle(project)
        recheck(bound)
        idle = _compile_idle(project, source, verify, bound)
        if idle is None:
            return {'state': 'deferred', 'reason': 'compile_refresh_ledger_unavailable'}
        if idle:
            # A lifecycle pass that left nothing to compile skips the worker.
            return {'state': 'drained', 'processed': 0}
    proc = subprocess.run(
        [sys.executable, '-B', str(Path(__file__).with_name('wiki_compile_worker.py')),
         '--cwd', str(project), '--json', '--flush-all'],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
        env={**os.environ, 'QMD_RUNTIME_DISPATCH': '1'})
    recheck(bound)
    if proc.returncode:
        return {'state': 'deferred', 'reason': 'compile_worker_failed',
                'detail': _worker_failure_code(proc.stderr)}
    try:
        answer = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return {'state': 'deferred', 'reason': 'compile_worker_result_invalid'}
    if isinstance(answer, dict) and answer.get('state') == 'deferred':
        if (isinstance(answer.get('reason'), str)
                and answer['reason'] in _DEFERRED_REASONS | {'resource_deferred'}):
            return {'state': 'deferred', 'reason': answer['reason']}
        return {'state': 'deferred', 'reason': 'compile_worker_result_invalid'}
    if not isinstance(answer, dict) or type(answer.get('processed')) is not int:
        return {'state': 'deferred', 'reason': 'compile_worker_result_invalid'}
    idle = _compile_idle(project, source, verify, bound)
    if idle is None:
        return {'state': 'deferred', 'reason': 'compile_refresh_ledger_unavailable'}
    if not idle:
        return {'state': 'pending', 'processed': answer['processed']}
    return {'state': 'drained', 'processed': answer['processed']}


def _queue_busy(queue: Path):
    """True if ``queue`` holds rows or a live claim, decided under its lock.

    Dead-owner ``<queue>.claimed.*`` rows are requeued first. Claim presence and
    queue content are read under the same lock ``claim_queue`` moves rows
    under, so a concurrent claim is seen as either the queue or its claim.
    """
    if not queue.parent.is_dir():
        return False
    import fcntl
    from wiki_compile_enqueue import _queue_lock_path
    from wiki_compile_worker import reclaim_orphaned_claimed
    prefix = queue.name + '.claimed.'
    with open(_queue_lock_path(queue), 'a', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            reclaim_orphaned_claimed(queue)
            if any(child.name.startswith(prefix) for child in queue.parent.iterdir()):
                return True
            return bool(queue.exists() and queue.stat().st_size)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _compile_idle(project: Path, source: Path, verify: Path, bound):
    """True when no worker, queue row, claim or active refresh is left; None if unknown."""
    import wiki_compile_worker as wcw
    run_lock = None
    try:
        if (source.parent / wcw.COMPILE_RUN_LOCK).exists():
            # A running worker moves rows between queues; hold its run lock so
            # none starts while the refresh ledger and both queues are read.
            run_lock = wcw.try_run_lock(project)
            if run_lock is None:
                return False
        return _idle_locked(project, source, verify, bound)
    except OSError:
        return None
    finally:
        if run_lock is not None:
            os.close(run_lock)


def _idle_locked(project: Path, source: Path, verify: Path, bound):
    from memvane.wiki.card import wiki_freshness
    unresolved = wiki_freshness.unresolved_pending_refreshes_strict(project)
    if unresolved is None:
        return None
    # A selected policy may exclude a source after its old refresh event was
    # recorded. Keep the ledger for reversible re-enrollment, but do not hold
    # the selected compile wake open for a source the worker must not compile.
    from memvane.settings.source_exclusion import excluded_source
    active_unresolved = [item for item in unresolved if not excluded_source(
        item['sourcePath'], bound[0]['recallSettings']['skipPaths'])]
    if active_unresolved:
        return False
    return not (_queue_busy(source) | _queue_busy(verify))
