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
from memvane.settings import api as settings
from memvane.store.api import regular
from memvane.store.types import MAX_BODY
from memvane.wiki.workspace import api as workspace, types as workspace_types
from memvane.wiki.verify import wiki_verify_worker as verify
from memvane.wiki.verify.pending_replacement import (
    _VERIFY_QUEUE_BYTES, job_revisions, pending_matches_job, pending_replacement)


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


def _replace(queue, lines):
    """Return only after an atomic rename. An exception leaves the old queue intact."""
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=queue.parent,
                                     prefix='.verify-queue-', delete=False) as tmp:
        try:
            if queue.exists():
                os.fchmod(tmp.fileno(), stat.S_IMODE(queue.stat().st_mode))
            tmp.writelines(lines)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, queue)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def _receipt(root, wiki, target, previous, selected, config):
    """A cleanup-only job understood by all four releases, bound to these bytes."""
    staged = pending_replacement(root, wiki, previous, target)
    if staged is None or previous.get('_requeue_count'):
        return None
    path, _ = staged
    try:
        raw, node, _ = regular(wiki, path.relative_to(wiki).as_posix(), MAX_BODY)
    except FileNotFoundError:
        return None
    text = raw.decode()
    if not pending_matches_job(text, previous):
        return None  # No proof of ownership. Preserve an edited private file.
    fingerprint = [node[k] for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')]
    marker = {'fingerprint': fingerprint, 'jobKey': verify._cleanup_job_key(previous)}
    if selected is not None and verify.parse_frontmatter(text)[0].get('status') == 'verified':
        verify._supersede_job_intent(root, config, previous, path, selected[0])
    freshness = verify.source_freshness(root, config, previous) or {}
    reason = ('pending_source_changed' if freshness.get('reason') == 'content_hash_mismatch'
              else 'pending_replacement_superseded')
    record = {**verify.base_record(previous), 'targetPath': previous['pendingPath'], 'reasons': [reason]}
    # The existing untrimmed deletion ledger must explain every retired candidate.
    if not verify.record_verify_deletion(root, {}, job_revisions(previous), record, 'stale_replacement'):
        raise OSError('pending_retirement_audit_failed')
    return {**previous, '_pendingCleanup': marker}, path, reason


def _fingerprint(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _unlink_if_same(path, fingerprint):
    try:
        if _fingerprint(path) == fingerprint:
            path.unlink()
    except OSError:
        pass  # Keep uncertain bytes. Retired files also have a queued cleanup receipt.


def enqueue(root, config, wiki, queue, job, selected, stage):
    """Stage first, then atomically replace obsolete jobs by cleanup receipts and this job."""
    target = root / job['targetPath']
    try:
        with workspace.queue_lock_path(queue).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
            retained, retired = [], {}
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
                    continue
                if path in receipts:
                    # Saved fingerprints take precedence over every replay of the
                    # job. Even conflicting receipts go through the verifier as-is.
                    continue
                if path not in retired:
                    proof = _receipt(root, wiki, target, previous, selected, config)
                    if proof:
                        retired[path] = proof
            if retained and not retained[-1].endswith('\n'):
                retained[-1] += '\n'
            for receipt, _, _ in retired.values():
                retained.append(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + '\n')
            retained.append(json.dumps(job, ensure_ascii=False, sort_keys=True) + '\n')
            # Failed publication keeps all prior jobs and candidates retryable. The
            # staged candidate can be reused on the writer's retry.
            candidate = root / job['pendingPath']
            existed = candidate.exists()
            if not stage():
                return False
            fingerprint = _fingerprint(candidate)
            try:
                _replace(queue, retained)
            except OSError:
                if not existed:
                    _unlink_if_same(candidate, fingerprint)
                raise
            fsync_dir(queue.parent)
            for receipt, path, reason in retired.values():
                # A crash or unlink failure leaves the durable cleanup-only receipt.
                if receipt['pendingPath'] not in referenced:
                    _unlink_if_same(path, receipt['_pendingCleanup']['fingerprint'])
                # Diagnostic only. Publication and the durable deletion audit already succeeded.
                try:
                    log = settings.ledger(root, workspace_types.VERIFY_LOG)
                    if log is not None:
                        verify.log_verdict(log, {**verify.base_record(receipt),
                                                 'result': 'stale_replacement', 'reason': reason})
                except OSError:
                    pass
                workspace.append_activity(root, 'stale_replacement', path.relative_to(wiki).as_posix(),
                                          '', bound=selected, reason=reason)
            return True
    except (OSError, RuntimeError, ValueError):
        return False
