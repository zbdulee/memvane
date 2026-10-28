"""Selected Laya wiki compile boundary, shared by enqueue, writer and verifier.

The installer journal authorizes the route. A project setting or an environment
variable alone never authorizes a selected write or a fallback to legacy QMD.
"""
from __future__ import annotations

from pathlib import Path
import re
import json
import itertools
import os
import subprocess
import sys

import source_text


def selected(root: Path):
    from laya_index_host_route import select_route
    from laya_install_journal import verify_selected_route

    root = Path(root).resolve()
    decision = select_route(str(root), action='compile')
    if decision.get('state') == 'legacy':
        return None
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(root):
        raise ValueError('selected_compile_route_unavailable')
    proof = verify_selected_route(decision)
    if proof.get('productRouteAllowed') is not True:
        raise ValueError('selected_compile_journal_unavailable')
    collections = decision.get('collections')
    if (not isinstance(collections, list) or len(collections) != 2 or
            sorted(item.get('role') for item in collections if isinstance(item, dict))
            != ['raw', 'wiki']):
        raise ValueError('selected_compile_collections_unavailable')
    return decision, proof


def recheck(bound):
    if bound is None:
        return
    from laya_install_journal import verify_selected_route
    if verify_selected_route(bound[0]) != bound[1]:
        raise ValueError('selected_compile_proof_changed')


def route_identity(bound):
    if bound is None:
        return None
    decision = bound[0]
    return {key: decision[key] for key in ('installId', 'generation',
                                           'scopeId', 'dbIncarnation')}


def request_compile(root: Path, bound):
    if bound is None:
        return
    recheck(bound)
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
    """Bounded local retrieval for compile grounding and judge candidates.

    Retrieval is a hint, not an authority for canonical status. The existing
    duplicate judge still decides whether a candidate needs review. A bounded
    read also sees cards created in this worker before the next index publish.
    """
    bound = selected(root) if bound is None else bound
    if bound is None:
        raise ValueError('selected_compile_required')
    recheck(bound)
    approved = next(item['sourceRoot'] for item in bound[0]['collections']
                    if item['role'] == 'wiki')
    if str(wiki_root.resolve()) != approved:
        raise ValueError('selected_wiki_root_changed')
    if (type(top_k) is not int or not 1 <= top_k <= 10 or
            type(cap_chars) is not int or not 1 <= cap_chars <= 12000):
        raise ValueError('selected_wiki_retrieval_budget')
    terms = set(re.findall(r'\w+', text.casefold()))
    if not terms:
        return []
    files = sorted(itertools.islice(wiki_root.rglob('*.md'), 201))
    if len(files) > 200:
        raise ValueError('selected_wiki_retrieval_capacity')
    matches = []
    skip_paths = bound[0]['recallSettings']['skipPaths']
    total_bytes = 0
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('selected_wiki_source_unsafe')
        rel = path.relative_to(wiki_root)
        # Wiki-root meta pages left by earlier releases are not cards; they must
        # never ground an extractor prompt or a duplicate judgment.
        if (any(part.startswith('.') for part in rel.parts)
                or source_text.is_wiki_meta_page(rel)):
            continue
        size = path.stat().st_size
        total_bytes += size
        if size > 5_000_000 or total_bytes > 5_000_000:
            raise ValueError('selected_wiki_source_oversize')
        body = path.read_text(encoding='utf-8')
        if skip_paths:
            # Grounding and duplicate review must not feed a card from an
            # excluded raw folder to a paid extractor. Mixed-source cards are
            # withheld as a whole; no file is deleted or rewritten.
            from laya_source_exclusion import card_sources_allowed
            if not card_sources_allowed(body, skip_paths):
                continue
        words = set(re.findall(r'\w+', body.casefold()))
        common = len(terms & words)
        if common:
            matches.append((path, common / max(1, len(terms)), body[:cap_chars]))
    recheck(bound)
    matches.sort(key=lambda row: (-row[1], str(row[0])))
    return matches[:top_k]


def run(project: Path):
    """One bounded durable compile/verify pass; ACK only drained queues."""
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        return {'state': 'deferred', 'reason': 'selected_project_unavailable'}
    bound = selected(project)
    if bound is None or bound[0]['projectRoot'] != str(project):
        return {'state': 'deferred', 'reason': 'selected_compile_route_unavailable'}
    from wiki_compile_enqueue import _safe_queue_path
    import compile_paths as cp
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
        return {'state': 'deferred', 'reason': 'compile_worker_failed'}
    try:
        answer = json.loads(proc.stdout)
    except (ValueError, TypeError):
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
    import wiki_freshness
    unresolved = wiki_freshness.unresolved_pending_refreshes_strict(project)
    if unresolved is None:
        return None
    # A selected policy may exclude a source after its old refresh event was
    # recorded. Keep the ledger for reversible re-enrollment, but do not hold
    # the selected compile wake open for a source the worker must not compile.
    from laya_source_exclusion import excluded_source
    active_unresolved = [item for item in unresolved if not excluded_source(
        item['sourcePath'], bound[0]['recallSettings']['skipPaths'])]
    if active_unresolved:
        return False
    return not (_queue_busy(source) | _queue_busy(verify))
