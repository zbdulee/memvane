"""Selected wiki compile and retrieval execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.wiki.workspace.api import selected, recheck, queue_busy as _queue_busy, deferred_result
from memvane.wiki.workspace.types import DEFERRED_REASONS as _DEFERRED_REASONS

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
    from memvane.wiki.compile import wiki_compile_retrieval
    matches = wiki_compile_retrieval.matches(root, wiki_root, text, top_k,
                                             cap_chars, bound)
    recheck(bound)
    return matches


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
    from memvane.wiki.workspace.queue_paths import _safe_queue_path
    from memvane.wiki.workspace import compile_paths as cp
    source = _safe_queue_path(project, cp.rel(cp.SOURCE_QUEUE))
    verify = _safe_queue_path(project, cp.rel(cp.VERIFY_QUEUE))
    if source is None or verify is None:
        return {'state': 'deferred', 'reason': 'unsafe_compile_queues'}
    from memvane.wiki.lifecycle import api as wiki_lifecycle_request
    from memvane.wiki.workspace.lifecycle_requests import claim as claim_lifecycle
    try:
        lifecycle = claim_lifecycle(project)
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
    from memvane.kernel.bundle_paths import entry_argv
    proc = subprocess.run(
        entry_argv('wiki-compile-worker', '--cwd', str(project), '--json', '--flush-all'),
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


def _compile_idle(project: Path, source: Path, verify: Path, bound):
    """True when no worker, queue row, claim or active refresh is left; None if unknown."""
    from memvane.wiki.compile import wiki_compile_worker as wcw
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
    from memvane.settings.api import excluded_source
    active_unresolved = [item for item in unresolved if not excluded_source(
        item['sourcePath'], bound[0]['recallSettings']['skipPaths'])]
    if active_unresolved:
        return False
    from memvane.wiki.intake.api import write_discard_receipt
    return not (_queue_busy(source, receipt_writer=write_discard_receipt) | _queue_busy(verify))
