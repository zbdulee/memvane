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
    total_bytes = 0
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('selected_wiki_source_unsafe')
        path.relative_to(wiki_root)
        if any(part.startswith('.') for part in path.relative_to(wiki_root).parts):
            continue
        size = path.stat().st_size
        total_bytes += size
        if size > 5_000_000 or total_bytes > 5_000_000:
            raise ValueError('selected_wiki_source_oversize')
        body = path.read_text(encoding='utf-8')
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
    import wiki_freshness
    unresolved = wiki_freshness.unresolved_pending_refreshes_strict(project)
    if unresolved is None:
        return {'state': 'deferred', 'reason': 'compile_refresh_ledger_unavailable'}
    if ((source.exists() and source.stat().st_size) or
            (verify.exists() and verify.stat().st_size) or unresolved):
        return {'state': 'pending', 'processed': answer['processed']}
    return {'state': 'drained', 'processed': answer['processed']}
