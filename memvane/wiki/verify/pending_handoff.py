"""Publish one reviewable replacement per target to every 1.x verifier.

The writer holds the card and wiki mutation locks. The verify queue lock
also excludes claim and requeue. An active claim can still put its saved job
back later, so the writer leaves its source job retryable until it drains.
"""
from __future__ import annotations

import fcntl
from itertools import islice
import json
import os
from pathlib import Path
import stat
import tempfile

from memvane.kernel.atomic_rename import fsync_dir
from memvane.settings import api as settings, types as settings_types
from memvane.store.api import regular
from memvane.wiki.workspace import api as workspace
from memvane.wiki.verify import pending_ownership as ownership
from memvane.wiki.verify import wiki_verify_worker as verify
from memvane.wiki.verify.pending_replacement import (
    _VERIFY_QUEUE_BYTES, job_revisions, pending_replacement)


def _rows(queue, limit):
    try:
        data, _, _ = regular(queue.parent, queue.name, limit)
    except FileNotFoundError:
        return [], 0
    rows = []
    for line in data.decode().splitlines(keepends=True):
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        rows.append((line, value if isinstance(value, dict) else {}))
    return rows, len(data)


def _stage_queue(queue, lines):
    """Persist both the queue image and its directory entry before recording the plan."""
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=queue.parent,
                                     prefix='.verify-swap-', delete=False) as tmp:
        try:
            if queue.exists():
                os.fchmod(tmp.fileno(), stat.S_IMODE(queue.stat().st_mode))
            tmp.writelines(lines)
            tmp.flush()
            os.fsync(tmp.fileno())
            fsync_dir(queue.parent)
        except OSError:
            Path(tmp.name).unlink(missing_ok=True)
            raise
    return Path(tmp.name)


def _fingerprint(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _unlink_if_same(path, fingerprint, produced):
    if produced is None:
        return
    try:
        raw, _, _ = regular(path.parent, path.name, len(produced))
        if raw == produced and _fingerprint(path) == fingerprint:
            path.unlink()
    except (OSError, ValueError):
        pass  # Keep any bytes whose identity changed during the failed write.


def enqueue(root, config, wiki, queue, job, selected, stage, *, produced_bytes=None):
    try:
        with workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return enqueue_locked(root, config, wiki, queue, job, selected, stage,
                                  produced_bytes=produced_bytes)
    except (OSError, RuntimeError, ValueError):
        return False


def enqueue_locked(root, config, wiki, queue, job, selected, stage, *, produced_bytes=None):
    """Caller owns the card, mutation and verify queue locks. Legacy jobs carry no byte proof."""
    target = root / job['targetPath']
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        return False
    limit = _VERIFY_QUEUE_BYTES
    referenced = set()
    for claim in claims:
        rows, size = _rows(claim, limit)
        limit -= size
        referenced.update(other['pendingPath'] for _, other in rows if isinstance(other.get('pendingPath'), str))
        if any(other.get('targetPath') == job['targetPath'] for _, other in rows):
            return False
    rows, _ = _rows(queue, limit)
    receipts = {row['pendingPath'] for _, row in rows if isinstance(row.get('pendingPath'), str)
                and row.get('targetPath') == job['targetPath'] and verify._valid_cleanup_marker(row)}
    if job['pendingPath'] in receipts and (root / job['pendingPath']).exists():
        return False  # A cleanup-only file must never be turned back into a paid review.
    if job.get('sourceRevisions'):
        if verify.source_freshness(root, config, job)['state'] != 'fresh':
            return False
    elif any(other.get('targetPath') == job['targetPath'] and job_revisions(other)
             for _, other in rows):
        return False  # An unbound candidate cannot supersede a revision-bound review.
    state = ownership.load(queue)
    ownership.discard_unpublished(queue, state)
    retained, preserved = [], []
    for raw, previous in rows:
        path = previous.get('pendingPath')
        if previous.get('targetPath') != job['targetPath']:
            if isinstance(path, str):
                referenced.add(path)
            retained.append(raw)
            continue
        if not isinstance(path, str) or path == job['pendingPath']:
            continue
        if '_pendingCleanup' in previous:
            # Outstanding cleanup receipts stay retryable. A completed receipt
            # need not accumulate across subsequent edits before verification.
            staged = pending_replacement(root, wiki, previous, target)
            if staged and staged[0].exists():
                retained.append(raw)
                referenced.add(path)
            continue
        if path in receipts:
            # Saved fingerprints take precedence over every replay of the
            # job. Even conflicting receipts go through the verifier as-is.
            continue
        reason = ownership.prepare(root, config, wiki, state, previous)
        if reason:
            preserved.append((previous, reason))
    if retained and not retained[-1].endswith('\n'):
        retained[-1] += '\n'
    retained.append(json.dumps(job, ensure_ascii=False, sort_keys=True) + '\n')
    # Failed publication keeps all prior jobs and candidates retryable. The
    # staged candidate can be reused on the writer's retry.
    candidate = root / job['pendingPath']
    existed = candidate.exists()
    if not stage():
        return False
    fingerprint = _fingerprint(candidate)
    swap = None
    try:
        swap = _stage_queue(queue, retained)
        ownership.remember(root, state, job, produced_bytes)
        for plan in state['retirements'].values():
            plan.setdefault('swap', swap.name)
        ownership.store(queue, state)
        os.replace(swap, queue)
    except (OSError, ValueError):
        if swap is not None and swap.exists():
            # A failed fsync may follow a successful state rename. Inspect its
            # durable contents before dropping the no-commit witness.
            try:
                fsync_dir(queue.parent)
                ownership.discard_unpublished(queue, ownership.load(queue))
                swap.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass  # Recovery keeps the witness until cancellation is durable.
        if not existed:
            _unlink_if_same(candidate, fingerprint, produced_bytes)
        raise
    fsync_dir(queue.parent)
    referenced.add(job['pendingPath'])
    for previous, reason in preserved:
        ownership.log(root, previous, selected, wiki, 'skipped', reason)
    ownership.finish(root, config, wiki, queue, state, referenced, selected)
    return True


def resume_retirements(root, config, queue, selected):
    """Recover a queue publication whose old-file audit or unlink is still due."""
    if queue is None:
        return True
    try:
        if not os.path.lexists(queue.with_name(ownership.NAME)):
            return True
        with workspace.card_write_lock(root), workspace.mutation_lock(root), workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = ownership.load(queue)
            ownership.discard_unpublished(queue, state)
            if not state['retirements']:
                return True
            refs = set()
            budget = _VERIFY_QUEUE_BYTES
            claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
            if len(claims) > 64:
                return False
            for item in [queue, *claims]:
                rows, size = _rows(item, budget)
                budget -= size
                refs.update(row['pendingPath'] for _, row in rows if isinstance(row.get('pendingPath'), str))
            if not (state['retirements'].keys() - refs):
                return True
            if selected is None:
                return False
            if config is None:
                config = settings.load_project_config(root)
            wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
            if (wiki == root or wiki.resolve() != wiki or not wiki.is_relative_to(root) or
                    not any(c.get('role') == 'wiki' and c.get('sourceRoot') == str(wiki)
                            for c in selected[0].get('collections', []))):
                return False
            workspace.recheck(selected)
            ownership.finish(root, config, wiki, queue, state, refs, selected)
            return not (state['retirements'].keys() - refs)
    except (OSError, RuntimeError, ValueError):
        return False  # An unreadable queue, plan, or audit cannot authorize deletion.
