"""Register replacement reviews privately while preserving released queue occurrences.

The writer holds the card and wiki mutation locks. The verify queue lock
also excludes claim and requeue. An active claim can still put its saved job
back later, so the writer leaves its source job retryable until it drains.
"""
from __future__ import annotations

import fcntl
import hashlib
from itertools import islice
import json
import os
from pathlib import Path

from memvane.wiki.verify import candidate_stage, deferred_review, native_review, queue_batch_prepare, queue_publication as publication
from memvane.wiki.verify.handoff_work import HOLD_SCAN_LIMIT, Work
from memvane.store.types import MAX_BODY
from memvane.settings import api as settings, types as settings_types
from memvane.wiki.workspace.handoff.api import evidence as regular
from memvane.wiki.workspace import api as workspace
from memvane.wiki.verify import pending_ownership as ownership
from memvane.wiki.verify import wiki_verify_worker as verify
from memvane.wiki.verify.pending_replacement import (
    handoff_freshness,
    _VERIFY_QUEUE_BYTES, job_revisions, pending_replacement)


def _rows(queue, limit):
    from memvane.wiki.workspace.handoff.api import bounded_rows, occurrence
    try:
        rows = [(raw, occurrence(raw)[0] or {}) for _, raw in bounded_rows(queue, max_bytes=limit)]
    except FileNotFoundError:
        return [], 0
    return rows, sum(len(raw) for raw, _ in rows)


def reserved(queue, rel, *, fresh=False):
    private, uncertain = native_review.references(queue)
    if rel in private or (uncertain and not fresh) or publication.reserved(queue, rel, fresh=fresh):
        return True
    try:
        groups = _references(queue)[1]
    except OSError:
        return not fresh
    for group in groups:
        if group.get('pendingPath') == rel:
            return True
    return False


def _references(queue, *, exclude_parent=None):
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        raise OSError('verify claim limit')
    budget = _VERIFY_QUEUE_BYTES
    groups = []
    for path in (queue, *claims):
        rows, used = _rows(path, budget)
        budget -= used
        groups.extend(job for _, job in rows)
    refs = {row['pendingPath'] for row in groups if isinstance(row.get('pendingPath'), str)}
    held_refs = retained_references(queue, exclude_parent=exclude_parent)
    if held_refs is None or any(not row or not isinstance(row.get('targetPath'), str) for row in groups):
        return None, groups
    return refs | held_refs, groups


def retained_references(queue, *, exclude_parent=None, exclude_work=None):
    holds = Work.entries(queue, 'hold', max_entries=HOLD_SCAN_LIMIT)
    refs = set()
    opaque = False
    for held in holds:
        state = held.state
        binding = state['binding']
        opaque |= bool(binding.get('opaque'))
        if (state['phase'] != 'finished' and binding.get('purpose') == 'verify'
                and 'image' not in state['blobs']):
            rel = binding['job'].get('pendingPath')
            if isinstance(rel, str):
                refs.add(rel)
    private, uncertain = native_review.references(queue, exclude_parent=exclude_parent, exclude_work=exclude_work)
    refs.update(private)
    complete = (not uncertain and not opaque and not holds.issues and not queue_batch_prepare.unresolved(queue, holds)
                and not queue_batch_prepare.opaque_ranges(queue, holds))
    return refs if complete else None


def _escrow(root, wiki, queue, work, removed, target):
    if not removed:
        return []
    project_rel = target.relative_to(root).as_posix()
    wiki_rel = target.relative_to(wiki).as_posix()
    if '..' in target.parts or target.resolve() != target or any(part.startswith('.') for part in Path(wiki_rel).parts):
        raise ValueError('invalid verify target')
    base, _, _ = regular(wiki, wiki_rel, MAX_BODY)
    tx = work.state['id']
    base_work = Work.create(queue, 'hold', {'purpose': 'base', 'parent': tx, 'targetPath': project_rel}, base)
    base_work.read('raw')
    base_work.commit('held', transaction=tx)
    holds = [base_work.state['id']]
    for ordinal, offset, raw, previous in removed:
        image = None
        if isinstance(previous.get('pendingPath'), str):
            try:
                path, _ = pending_replacement(root, wiki, previous, target)
            except ValueError:
                path = None  # An invalid target cannot grant access to another file.
            if path is not None:
                try:
                    image, _, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
                except FileNotFoundError:
                    pass  # A missing candidate leaves only the original row as evidence.
        binding = {'purpose': 'verify', 'parent': tx, 'base': base_work.state['id'], 'job': previous,
                   'offset': offset, 'ordinal': ordinal, 'targetPath': project_rel}
        if not isinstance(previous.get('targetPath'), str) or not raw.endswith(b'\n'):
            binding['opaque'] = True
        hold = Work.create(queue, 'hold', binding, raw, image=image)
        hold.read('raw')
        if image is not None:
            hold.read('image')
        hold.commit('held', transaction=tx)
        holds.append(hold.state['id'])
    return holds


def enqueue(root, config, wiki, queue, job, selected, stage, *, produced_bytes=None):
    try:
        with workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return enqueue_locked(root, config, wiki, queue, job, selected, stage,
                                  produced_bytes=produced_bytes)
    except (OSError, RuntimeError, ValueError):
        return False


def enqueue_locked(root, config, wiki, queue, job, selected, stage, *, produced_bytes=None, deferred_id=None):
    """Caller owns the card, mutation and verify queue locks. Legacy jobs carry no byte proof."""
    if queue_batch_prepare.unresolved(queue):
        return deferred_review.reserve(queue, job, produced_bytes, selected) if deferred_id is None else False
    target = root / job['targetPath']
    base, _, _ = regular(wiki, target.relative_to(wiki).as_posix(), MAX_BODY)
    if hashlib.sha256(base).hexdigest() != job['expectedTargetHash']:
        return False
    claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
    if len(claims) > 64:
        return deferred_review.reserve(queue, job, produced_bytes, selected) if deferred_id is None else False
    limit = _VERIFY_QUEUE_BYTES
    referenced = set()
    for claim in claims:
        rows, size = _rows(claim, limit)
        limit -= size
        referenced.update(other['pendingPath'] for _, other in rows if isinstance(other.get('pendingPath'), str))
        if any(other.get('targetPath') == job['targetPath'] for _, other in rows):
            return False
    before_node = publication._queue_node(queue)
    rows, _ = _rows(queue, limit)
    receipts = {row['pendingPath'] for _, row in rows if isinstance(row.get('pendingPath'), str)
                and row.get('targetPath') == job['targetPath'] and verify._valid_cleanup_marker(row)}
    for _, receipt in rows:
        if (receipt.get('pendingPath') in receipts and verify._valid_cleanup_marker(receipt)
                and receipt.get('targetPath') == job['targetPath']
                and receipt.get('expectedTargetHash') == job.get('expectedTargetHash')
                and receipt.get('sourceRevisions') == job.get('sourceRevisions')):
            old_candidate, _ = pending_replacement(root, wiki, receipt, target)
            if old_candidate.exists():
                # Giving the same revision a fresh filename cannot revive cleanup-only work.
                return False
    if job.get('sourceRevisions'):
        if handoff_freshness(root, config, job)['state'] != 'fresh':
            return False
    elif any(other.get('targetPath') == job['targetPath'] and job_revisions(other)
             for _, other in rows):
        return False  # An unbound candidate cannot supersede a revision-bound review.
    entries = Work.entries(queue, 'publication')
    if entries:
        references, _ = _references(queue)
        publication.recover(root, queue, entries, references, selected)
    state = ownership.load(queue)
    existing_plans = set(state['retirements'])
    retained, preserved = [], []
    removed = []
    offset = 0
    for ordinal, (raw, previous) in enumerate(rows):
        start, offset = offset, offset + len(raw)
        path = previous.get('pendingPath')
        if not isinstance(previous.get('targetPath'), str) or not raw.endswith(b'\n'):
            if claims:
                return False  # The retained public bytes may be a released owner's only reference.
            removed.append((ordinal, start, raw, previous))
            continue
        if previous.get('targetPath') != job['targetPath']:
            if isinstance(path, str):
                referenced.add(path)
            retained.append(raw)
            continue
        occurrence = (ordinal, start, raw, previous)
        if not isinstance(path, str) or path == job['pendingPath']:
            removed.append(occurrence)
            continue
        if '_pendingCleanup' in previous:
            # Outstanding cleanup receipts stay retryable. A completed receipt
            # need not accumulate across subsequent edits before verification.
            staged = pending_replacement(root, wiki, previous, target)
            if staged and staged[0].exists():
                retained.append(raw)
                referenced.add(path)
            else:
                removed.append(occurrence)
            continue
        removed.append(occurrence)
        if path in receipts:
            # Saved fingerprints take precedence over every replay of the
            # job. Even conflicting receipts go through the verifier as-is.
            continue
        freshness = handoff_freshness(root, config, previous) or {}
        if freshness.get('reason') != 'content_hash_mismatch':
            continue  # A second fresh source or unreadable evidence retains its independent obligation.
        reason = ownership.prepare(root, config, wiki, state, previous)
        if reason:
            preserved.append((previous, reason))
    if retained and not retained[-1].endswith(b'\n'):
        return False  # The intact source row owns its torn suffix.
    before = b''.join(raw for raw, _ in rows)
    work = publication.reserve(queue, job, selected, before, produced_bytes, produced=produced_bytes is not None,
                               deferred=deferred_id)
    candidate = root / job['pendingPath']
    try:
        holds = _escrow(root, wiki, queue, work, removed, target)
        if produced_bytes is None:
            work.commit('creating')
            if not stage():
                publication.cancel(root, queue, work, referenced)
                return False
            work.commit('staged')
        else:
            with candidate_stage.producer(work, candidate, produced_bytes):
                if not stage():
                    publication.cancel(root, queue, work, referenced)
                    return False
        image, node, _ = regular(wiki, candidate.relative_to(wiki).as_posix(), MAX_BODY)
        if produced_bytes is not None:
            recorded = work.state['candidate']
            if (image != produced_bytes or node['ino'] != recorded['inode'] or
                    node['mtime_ns'] != recorded['mtime']):
                raise OSError('replacement candidate changed before registration')
        holds.append(native_review.reserve(queue, work, job, image, node))
        retired = []
        for name, plan in state['retirements'].items():
            if name not in existing_plans:
                retired.append(dict(plan, swap=work.state['binding']['swap']))
        if work.read('raw') != before:
            raise OSError('verify handoff source changed')
        publication.publish(root, queue, work, retained, before_node, retired, holds)
    except (ValueError, RuntimeError, OSError):
        refs, _ = _references(queue)
        publication.recover(root, queue, [work], refs, selected)
        raise
    referenced.add(job['pendingPath'])
    state = ownership.load(queue)
    for previous, reason in preserved:
        ownership.log(root, previous, selected, wiki, 'skipped', reason)
    refs, _ = _references(queue)
    if refs is not None and not entries.issues:
        ownership.finish(root, config, wiki, queue, state, refs | referenced, selected)
    return True


def resume_retirements(root, config, queue, selected):
    """Recover a queue publication whose old-file audit or unlink is still due."""
    if queue is None:
        return True
    try:
        entries = Work.entries(queue, 'publication')
        if not entries and not ownership.has_state(queue):
            return True
        with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            entries = Work.entries(queue, 'publication')
            if entries:
                refs, _ = _references(queue)
                publication.recover(root, queue, entries, refs, selected)
            state = ownership.load(queue)
            if not state['retirements']:
                return True
            refs, _ = _references(queue)
            if refs is None or entries.issues:
                return False
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
