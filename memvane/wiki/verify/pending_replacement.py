"""Identity and queued review of a staged wiki replacement."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re

from memvane.settings import types as settings_types
from memvane.wiki.card import api as yaml_scalars, types as wiki_markers
from memvane.wiki.workspace import api as cp, types as ct


# Total bytes across the live verify queue and its claims for one reuse check.
_VERIFY_QUEUE_BYTES = 8 * 1024 * 1024


def pending_replacement(root: Path, wiki_root: Path, job: dict,
                        live_target: Path) -> tuple[Path, str] | None:
    """Accept only the compiler's hidden pending path for this exact target/job.

    ``sourceHash`` is the compiler's 16 lowercase hex card marker and
    the pending filename suffix.
    ``expectedTargetHash`` is the full SHA-256 of the prior live card bytes.
    """
    rel = job.get('pendingPath')
    if rel is None:
        return None
    job_hash = job.get('sourceHash')
    expected = job.get('expectedTargetHash')
    if (not isinstance(job_hash, str) or not re.fullmatch(r'[0-9a-f]{16}', job_hash)
            or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or not isinstance(rel, str)):
        raise ValueError('pending_replacement_invalid')
    root = root.resolve()
    pending = live_target.with_name('.' + live_target.name + '.pending-' + job_hash)
    if (pending.is_symlink() or pending.relative_to(root).as_posix() != rel
            or pending.parent != live_target.parent
            or not pending.is_relative_to(wiki_root)):
        raise ValueError('pending_replacement_invalid')
    return pending, expected


def job_revisions(job):
    raw = job.get('sourceRevisions')
    if not isinstance(raw, list) or not raw:
        return []
    revisions = [yaml_scalars.normalize_source_revision(item) for item in raw]
    return revisions if all(revisions) else []


def pending_matches_job(text, job):
    """The managed candidate names exactly the source snapshots in this job."""
    front = wiki_markers.FRONTMATTER_RE.match(text)
    marker = wiki_markers.AUTO_START_RE.search(text)
    revisions = job_revisions(job)
    meta, valid = yaml_scalars.parse_frontmatter(text)
    status = meta.get('status')
    return bool(valid and status in ('generated', 'verified')
                and (status != 'verified' or meta.get('verifiedBodyHash') == yaml_scalars.card_body_hash(text))
                and revisions and front and marker and marker.group(1) == job.get('sourceHash')
                and yaml_scalars.auto_writable_text(text)[0]
                and yaml_scalars.frontmatter_source_revisions(front.group(1)) == revisions)


def pending_source_review(root, config, revision, *, reuse=True):
    """Return (proved reusable target, claim naming this source) under the same bounded queue read."""
    from itertools import islice
    from memvane.store.api import regular
    from memvane.store.types import MAX_BODY

    queue = cp.safe_queue_path(root, cp.rel(ct.VERIFY_QUEUE))
    wiki = root / config.get('wikiPath', settings_types.WIKI_REL)
    claimed_source = False
    if queue is None or wiki.resolve() != wiki or not wiki.is_relative_to(root):
        return None, claimed_source
    try:
        with cp.card_write_lock(root), cp.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            remaining = _VERIFY_QUEUE_BYTES
            rows = []
            claims = list(islice(queue.parent.glob(queue.name + '.claimed.*'), 65))
            if len(claims) > 64:
                return None, claimed_source
            for path in ([queue, *claims] if reuse else claims):
                if not path.exists():
                    continue
                data, _, _ = regular(queue.parent, path.name, remaining)
                remaining -= len(data)
                for line in data.splitlines():
                    try:
                        job = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(job, dict):
                        continue
                    if path != queue and not claimed_source and isinstance(job.get('targetPath'), str):
                        claimed_source = any(item['path'] == revision['path'] and
                                             item['collection'] == revision['collection']
                                             for item in job_revisions(job))
                    if reuse and isinstance(job.get('pendingPath'), str):
                        rows.append(job)
            cleanup = {job['pendingPath'] for job in rows if '_pendingCleanup' in job}
            card_budget = 6 * MAX_BODY
            for job in reversed(rows):
                revisions = job_revisions(job)
                if (job['pendingPath'] in cleanup or not any(
                        all(item[k] == revision[k] for k in ('path', 'collection', 'sha256', 'size'))
                        for item in revisions)):
                    continue
                if len(revisions) > 1:
                    freshness = yaml_scalars.check_card(job, root, yaml_scalars.allow_roots_of(config))
                    if freshness.get('state') != wiki_markers.FRESH:
                        continue
                rel = job.get('targetPath')
                if not isinstance(rel, str) or Path(rel).is_absolute():
                    continue
                live = root / rel
                if (live.resolve() != live or not live.is_relative_to(wiki)
                        or any(part.startswith('.') for part in live.relative_to(wiki).parts)):
                    continue
                try:
                    pending, expected = pending_replacement(root, wiki, job, live)
                    data, _, _ = regular(wiki, pending.relative_to(wiki).as_posix(), min(MAX_BODY, card_budget))
                    card_budget -= len(data)
                    if not pending_matches_job(data.decode('utf-8'), job):
                        continue
                    old, _, _ = regular(wiki, live.relative_to(wiki).as_posix(), min(MAX_BODY, card_budget))
                    card_budget -= len(old)
                    if hashlib.sha256(old).hexdigest() == expected:
                        return rel, claimed_source
                except (OSError, ValueError):
                    continue
    except (OSError, RuntimeError, ValueError):
        pass  # Unproved reuse costs a new extraction; it never suppresses work.
    return None, claimed_source
