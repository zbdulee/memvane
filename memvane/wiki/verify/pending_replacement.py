"""Identity and queued review of a staged wiki replacement."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
import re

from memvane.kernel.atomic_rename import rename_noreplace
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
    from memvane.wiki.workspace.handoff.api import evidence as regular
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
                from memvane.wiki.workspace.handoff.api import bounded_rows, occurrence
                for _, line in bounded_rows(path, max_bytes=remaining):
                    remaining -= len(line)
                    job, _ = occurrence(line)
                    if job is None:
                        continue
                    if path != queue and not claimed_source and isinstance(job.get('targetPath'), str):
                        claimed_source = any(item['path'] == revision['path'] and
                                             item['collection'] == revision['collection']
                                             for item in job_revisions(job))
                    if reuse and isinstance(job.get('pendingPath'), str):
                        rows.append(job)
            from memvane.wiki.verify import native_review
            for work in native_review.waiting(queue):
                job = work.state['binding']['job']
                if native_review._alive(work.state['proof'].get('owner')):
                    claimed_source |= any(item['path'] == revision['path'] and
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


def card_state(target: Path) -> tuple[str | None, dict, str, str]:
    """(text, frontmatter, status, block sourceHash) — 읽기 실패 시 text None."""
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None, {}, "", ""
    meta, ok = yaml_scalars.parse_frontmatter(text)
    if not ok:
        meta = {}
    status = str(meta.get("status") or "").strip() or "generated"
    match = wiki_markers.AUTO_START_RE.search(text)
    return text, meta, status, (match.group(1) if match else "")


def incompatible_cards(paths, *, report=True):
    for path in paths:
        text, _, _, _ = card_state(path)
        if text is not None and not yaml_scalars.card_sources_allowed(text, report=report):
            return True
    return False


def handoff_freshness(root, config, job):
    """Use the same provenance resolution as the verifier with the pass's snapshot allowance."""
    from memvane.store.types import MAX_BODY
    from memvane.wiki.workspace.handoff.api import evidence
    if 'sourceRevisions' not in job:
        return None
    revisions = job_revisions(job)
    if not revisions:
        return {'state': 'unknown', 'reason': 'invalid_source_revision'}
    allowed = yaml_scalars.allow_roots_of(config)
    for revision in revisions:
        path, reason = yaml_scalars.resolve_existing_source(revision['path'], root, allowed)
        if path is None:
            return {'state': 'stale' if reason == 'missing' else 'unknown', 'reason': reason}
        try:
            material, _, _ = evidence(path.parent, path.name, MAX_BODY)
        except (OSError, ValueError):
            return {'state': 'unknown', 'reason': 'handoff_source_snapshot_unavailable'}
        if hashlib.sha256(material).hexdigest() != revision['sha256']:
            return {'state': 'stale', 'reason': 'content_hash_mismatch'}
    return {'state': 'fresh', 'checked': len(revisions)}


def source_freshness(root: Path, config: dict, job: dict) -> dict | None:
    """Check job-owned provenance, preserving legacy jobs without the field."""
    if "sourceRevisions" not in job:
        return None
    revisions = job.get("sourceRevisions")
    return yaml_scalars.check_card(
        {"sourceRevisions": revisions}, root, yaml_scalars.allow_roots_of(config))


def _cleanup_pending(root, snapshots, extra_rows, paths, uncertain, *, detach=None):
    """Called inside the card and queue locks, before the claim return commits."""
    if uncertain:
        return False
    from memvane.wiki.workspace.handoff.api import bounded_rows, occurrence, MAX_BYTES, MAX_ROWS
    referenced = set()

    def collect(raw):
        if raw and not raw.endswith(b'\n'):
            return False
        for line in raw.split(b'\n'):
            if not line.strip():
                continue
            job, _ = occurrence(line + b'\n')
            if job is None:
                return False
            pending = job.get('pendingPath')
            if isinstance(pending, str):
                referenced.add(root / pending)
        return True

    if not collect(extra_rows):
        return False
    size, count = MAX_BYTES - len(extra_rows), MAX_ROWS - extra_rows.count(b'\n')
    if len(paths) > 64 or size < 0 or count < 0:
        return False
    for path in paths:
        try:
            for _, row in bounded_rows(path, size, count):
                if not collect(row):
                    return False
                size -= len(row)
                count -= 1
        except OSError:
            return False
    deferred = False
    for path, old in snapshots.items():
        if path in referenced:
            deferred = True
            continue
        if detach is not None:
            if not detach(path, old):
                deferred = True
            continue
        if path.parent.resolve() != path.parent:
            continue
        try:
            info = path.lstat()
            current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and current == old:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return not deferred


def _owned_stale(aside, old, digest):
    from memvane.wiki.workspace.handoff.api import evidence
    st = aside.lstat()
    if ((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != old[:4]
            or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()):
        return False
    raw, _, _ = evidence(aside.parent, aside.name, old[2])
    return hashlib.sha256(raw).hexdigest() == digest


def _detach_stale(work, path, old):
    from memvane.wiki.verify.candidate_stage import _parent, _present
    proof = work.state['proof']
    evidence_key = proof['staleImage']
    aside = path.with_name('.pr-' + work.state['id'])
    with _parent(path, evidence_key['parentInode']) as parent:
        moved_here = False
        if not _present(aside):
            if not _present(path):
                os.fsync(parent)
                return True
            st = path.lstat()
            current = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if current != old or proof.get('staleDetached'):
                return True  # A different generation remains at the original name.
            rename_noreplace(parent, path.name, aside.name)
            moved_here = True
            os.fsync(parent)
        if _present(path):
            return False  # A new occupant and the detached occurrence must both remain available.
        if not _owned_stale(aside, old, evidence_key['sha256']):
            if not moved_here:
                return False
            rename_noreplace(parent, aside.name, path.name)
            os.fsync(parent)
            return True
        if not proof.get('staleDetached'):
            work.commit('held', proof=dict(proof, staleDetached=True))
        if not _owned_stale(aside, old, evidence_key['sha256']):
            return False
        os.unlink(aside.name, dir_fd=parent)
        os.fsync(parent)
        return True


def finish_stale_work(root, config, work, job, *, fingerprint=None):
    """Once audited, retry only cleanup of this exact obsolete candidate under the card lock."""
    from memvane.wiki.workspace.handoff.api import check_cleanup, evidence
    from memvane.wiki.verify import pending_handoff
    from memvane.wiki.verify.wiki_verify_worker import _cleanup_job_key, _valid_cleanup_marker
    path = root / job['pendingPath']
    try:
        if fingerprint is not None:
            if os.path.lexists(path.with_name('.pr-' + work.state['id'])):
                return False, True
            raw, node, directory = evidence(path.parent, path.name, fingerprint[2])
            identity = tuple(node[key] for key in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'))
            if identity != tuple(fingerprint):
                return False, True
            marker = {'fingerprint': list(fingerprint), 'jobKey': _cleanup_job_key(job)}
            image = {'parentInode': directory['ino'], 'sha256': hashlib.sha256(raw).hexdigest()}
            work.commit('held', proof=dict(work.state['proof'], staleCleanup=marker, staleImage=image))
        marker = work.state['proof']['staleCleanup']
        if not _valid_cleanup_marker(dict(job, _pendingCleanup=marker)):
            return False, True
    except (KeyError, OSError, ValueError):
        return False, True

    def checked(paths, unknown):
        refs = pending_handoff.retained_references(work.queue, exclude_work=work.state['id'])
        if refs is None or job['pendingPath'] in refs:
            return False
        aside = path.with_name('.pr-' + work.state['id'])
        if not work.state['proof'].get('staleDetached') and not os.path.lexists(aside) and os.path.lexists(path):
            freshness = source_freshness(root, config, job) or {}
            if freshness.get('reason') != 'content_hash_mismatch':
                if freshness.get('state') == 'fresh':
                    proof = {key: value for key, value in work.state['proof'].items()
                             if key not in ('staleCleanup', 'staleImage')}
                    work.commit('held', proof=proof)
                return False
        return _cleanup_pending(root, {path: tuple(marker['fingerprint'])}, b'', paths, unknown,
                                detach=lambda path, old: _detach_stale(work, path, old))
    try:
        finished = check_cleanup(work.queue, checked)
    except (KeyError, OSError, ValueError):
        return False, True
    return finished, not finished
