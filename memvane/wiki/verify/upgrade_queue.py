"""Prepare an already populated verify queue for a mixed-version handoff."""
from __future__ import annotations

import fcntl
import hashlib
from itertools import islice
from pathlib import Path

from memvane.wiki.workspace.handoff import api as queue_pass
from memvane.settings import api as settings, types as settings_types
from memvane.wiki.workspace.handoff.api import evidence as regular
from memvane.store.types import MAX_BODY
from memvane.wiki.verify import pending_handoff as handoff, queue_batch_prepare
from memvane.wiki.verify import wiki_verify_worker as verifier
from memvane.wiki.verify.pending_replacement import (
    handoff_freshness,
    _VERIFY_QUEUE_BYTES, pending_matches_job, pending_replacement)
from memvane.wiki.workspace import api as workspace


def _competing(rows):
    groups, cleanup = {}, set()
    for _, job in rows:
        target, pending = job.get('targetPath'), job.get('pendingPath')
        if not isinstance(target, str) or not isinstance(pending, str):
            continue
        if '_pendingCleanup' in job:
            if verifier._valid_cleanup_marker(job):
                cleanup.add((target, pending))
        else:
            groups.setdefault(target, []).append(job)
    for target, jobs in groups.items():
        yield target, [job for job in jobs if (target, job['pendingPath']) not in cleanup]


def _current(root, config, wiki, relative, jobs):
    rel = Path(relative)
    if not rel.parts or rel.is_absolute() or '..' in rel.parts or '\x00' in relative:
        return None
    target = root / rel
    if (target == wiki or not target.is_relative_to(wiki) or target.resolve() != target or
            any(p.startswith('.') for p in target.relative_to(wiki).parts)):
        return None
    try:
        live, _, _ = regular(wiki, target.relative_to(wiki).as_posix(), MAX_BODY)
    except FileNotFoundError:
        return None
    digest = hashlib.sha256(live).hexdigest()
    # Physical FIFO breaks ties between independently fresh candidates on one base.
    for job in jobs:
        if (job.get('expectedTargetHash') != digest or
                (handoff_freshness(root, config, job) or {}).get('state') != 'fresh'):
            continue
        try:
            pending, _ = pending_replacement(root, wiki, job, target)
            raw, _, _ = regular(wiki, pending.relative_to(wiki).as_posix(), MAX_BODY)
            if pending_matches_job(raw.decode(), job):
                return job
        except (FileNotFoundError, ValueError):
            continue
    return None


@queue_pass.bounded
def prepare(root, queue, bound, config=None):
    """Resume an interrupted retirement, then normalize competing jobs before any legacy claim."""
    if queue is None:
        return True
    try:
        if (queue.parent / '.handoff' / 'v1' / 'hold').exists():
            from memvane.wiki.verify.handoff_work import Work
            try:
                with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), workspace.queue_lock_path(queue).open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    Work.reclaim_orphans(queue, 'hold')
            except OSError:
                pass
        if not queue_batch_prepare.prepare(root, queue, bound):
            return False
        from memvane.wiki.verify import deferred_review
        if deferred_review.active(queue):
            if config is None:
                config = settings.load_project_config(root)
            wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
            with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), workspace.queue_lock_path(queue).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                deferred_review.resume(root, config, wiki, queue, bound)
        # An unrelated audit must not block a live review, but an empty queue with an
        # unfinished retirement still needs its durable wake.
        retired = handoff.resume_retirements(root, config, queue, bound)
        if not queue.exists() or queue.stat().st_size == 0:
            # A dead verifier's claim can hold the last references. Allow native
            # claim recovery to drain it before making its audit a retry gate.
            from memvane.wiki.verify.native_review import waiting
            return retired or workspace.queue_busy(queue) or bool(waiting(queue))
        if bound is None:
            return True
        with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if queue.stat().st_size > _VERIFY_QUEUE_BYTES:
                return False  # Appended after the batch preflight; retry under its adoption lock.
            rows, used = handoff._rows(queue, _VERIFY_QUEUE_BYTES)
            groups = list(_competing(rows))
            claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
            if len(claims) > 64:
                return False
            claimed = set()
            for claim in claims:
                if claim.stat().st_size > _VERIFY_QUEUE_BYTES - used:
                    return False
                saved, size = handoff._rows(claim, _VERIFY_QUEUE_BYTES - used)
                used += size
                claimed.update(row['targetPath'] for _, row in saved if isinstance(row.get('targetPath'), str))
            for target, jobs in groups:
                if target in claimed:
                    continue  # The owner may still return its job. Native claim recovery must run.
                if config is None:
                    config = settings.load_project_config(root)
                wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
                if (wiki == root or wiki.resolve() != wiki or not wiki.is_relative_to(root) or
                        not any(c.get('role') == 'wiki' and c.get('sourceRoot') == str(wiki)
                                for c in bound[0].get('collections', []))):
                    return False
                job = _current(root, config, wiki, target, jobs)
                if job is None:
                    continue  # No proven current candidate. Let the current verifier settle these jobs.
                workspace.recheck(bound)
                if not handoff.enqueue_locked(root, config, wiki, queue, job, bound, lambda: True):
                    return False
            if not claims and queue.exists() and queue.stat().st_size:
                from memvane.wiki.workspace.handoff.api import withdraw
                from memvane.wiki.verify.ordinary_slot import preserve_pending
                if config is None:
                    config = settings.load_project_config(root)
                wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
                preserve_pending(root, wiki, queue, handoff._rows(queue, _VERIFY_QUEUE_BYTES)[0])
                workspace.recheck(bound)
                withdraw(queue)
            elif claims and rows and any(not queue_batch_prepare._dead(queue, path) for path in claims):
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False
