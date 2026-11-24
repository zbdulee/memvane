"""Settle a released singleton from its existing verified card when no new private control can be written."""
from __future__ import annotations

import errno
import hashlib

from memvane.store.types import MAX_BODY
from memvane.wiki.card import api as card
from memvane.wiki.verify import native_review
from memvane.wiki.verify.handoff_work import Work
from memvane.wiki.verify.ordinary_slot import safe_outputs
from memvane.wiki.verify.pending_replacement import handoff_freshness, pending_matches_job
from memvane.wiki.workspace import api as workspace
from memvane.wiki.workspace.handoff import api as files


def _observed(root, wiki, queue, config, selected):
    if next(queue.parent.glob(queue.name + '.claimed.*'), None) is not None:
        return None
    before = queue.lstat()
    if not 0 < before.st_size <= files.SLAB:
        return None
    rows = list(files.bounded_rows(queue, files.SLAB, 1))
    if len(rows) != 1:
        return None
    raw = rows[0][1]
    job, released_safe = files.occurrence(raw)
    if (not job or not released_safe or any(k in job for k in ('pendingPath', '_pendingCleanup', 'sourceFingerprints'))
            or not isinstance(job.get('targetPath'), str) or not safe_outputs(job)):
        return None
    target = root / job['targetPath']
    if not target.is_relative_to(wiki) or target == wiki or target.resolve() != target:
        return None
    data, node, _ = files.evidence(wiki, target.relative_to(wiki).as_posix(), MAX_BODY)
    text = data.decode()
    fm, ok = card.parse_frontmatter(text)
    skips = (selected[0].get('recallSettings') or {}).get('skipPaths', []) if selected else []
    if (not ok or fm.get('status') != 'verified' or not pending_matches_job(text, job)
            or not card.card_sources_allowed(job, skips) or not card.card_sources_allowed(text, skips)
            or (handoff_freshness(root, config, job) or {}).get('state') != 'fresh'):
        return None
    return (before.st_dev, before.st_ino, before.st_size, before.st_ctime_ns, before.st_mtime_ns), raw, job, node, hashlib.sha256(data).hexdigest()


def settle(root, wiki, queue, config, selected):
    try:
        seen = _observed(root, wiki, queue, config, selected)
        if seen is None:
            return False
        with workspace.card_write_lock(root, blocking=False), workspace.mutation_lock(root, blocking=False), native_review._locked(queue, blocking=False):
            if seen != _observed(root, wiki, queue, config, selected):
                return False
            stamp, raw, job, node, digest = seen
            workspace.recheck(selected)
            binding = dict(purpose='verified-occurrence', inode=stamp[1], offset=0,
                           digest=hashlib.sha256(raw).hexdigest(), targetPath=job['targetPath'])
            try:
                work = Work.create(queue, 'hold', binding, raw)
                work.commit('held', proof={'sha256': digest, 'node': node}).commit('finished').archive()
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
            # The durable verified stamp, managed body and every current revision still prove this occurrence.
            if seen != _observed(root, wiki, queue, config, selected):
                return False
            workspace.recheck(selected)
            return files.settle_observed(queue, stamp, raw)
    except (OSError, UnicodeError, TypeError, RuntimeError, ValueError):
        return False
