"""Revision-scoped cancel of one pending synthetic-QA ``update`` wake.

For a release-QA fixture project whose SessionStart update keeps failing after
the preserve-first worker handoff, only the exact pending runtime wake is
retired; the per-project update job and status files are never edited or
removed. Ownership comes from official private state, never from caller
strings alone: the release-QA fixture markers (``runtime_wake_cancel``'s
``fixture_proof``), a private project directory, and the private per-project
``<sha256(project)[:32]>.job.json`` (exact schema/cwd/hash, owner, mode, one
link) plus its optional ``.status.json``. The wake id must be
``wake_key('update', <that key>)``.

Locks are non-blocking and taken in the drain order -- UID dispatch lease
(``runtime-worker.lock``), then the per-job worker lock (``<key>.lock``) -- and
held across the verified online backup and the single transaction that
CAS-deletes the full row, removes a coalesced overflow row and appends an
audit row. A running worker, a busy dispatcher, a changed row/job/status or a
re-enqueued revision is refused, so a newer revision always survives. Replay
never restores the cancelled revision: new work is an explicit official
request (SessionStart or the update skill), which offers a fresh revision.
"""
from __future__ import annotations

from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import runtime_wake_policy as wake_policy
from runtime_wake_cancel import (AUDIT_TABLE, ROW_VERSION, CancelRefused, _backup,
                                 _private_file, _project, _refuse, _row, fixture_proof)

KIND = 'update'
JOB_SCHEMA = 'qmd-update-hook-job-v1'
STATUS_SCHEMA = 'qmd-update-hook-status-v1'
# Same bound update_hook_queue applies to its SessionStart input.
MAX_STATE_BYTES = 65536


def job_key(project):
    """update_hook_queue's per-project key and the update wake target."""
    return hashlib.sha256(str(project).encode()).hexdigest()[:32]


def _private_json(path):
    """Owner-only single-link JSON object; returns (value, sha256, stat)."""
    info = _private_file(path)
    if info.st_size > MAX_STATE_BYTES:
        _refuse('update_state_invalid')
    data = path.read_bytes()
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise CancelRefused('update_state_invalid') from exc
    if not isinstance(value, dict):
        _refuse('update_state_invalid')
    return value, hashlib.sha256(data).hexdigest(), info


def ownership_proof(project_raw):
    """Read-only proof that the wake target is an owned QA project's update job."""
    project = _project(project_raw)
    if project.stat().st_mode & 0o077:
        _refuse('project_not_private')
    fixture_id = fixture_proof(project)
    import update_hook_queue
    try:
        queue = update_hook_queue._private_queue()
    except (OSError, ValueError) as exc:
        raise CancelRefused('unsafe_update_queue') from exc
    key = job_key(project)
    job_path = queue / (key + '.job.json')
    if not (job_path.exists() or job_path.is_symlink()):
        _refuse('update_job_missing')
    job, job_sha, job_info = _private_json(job_path)
    if job != {'schema': JOB_SCHEMA, 'cwd': str(project)} or job_key(job['cwd']) != key:
        _refuse('update_job_invalid')
    status_path = queue / (key + '.status.json')
    status = None
    if status_path.exists() or status_path.is_symlink():
        value, status_sha, _ = _private_json(status_path)
        if value.get('schema') != STATUS_SCHEMA or value.get('cwdHash') != key:
            _refuse('update_status_invalid')
        status = {'sha256': status_sha, 'status': value.get('status'), 'runId': value.get('runId')}
    proof = {'project': str(project), 'fixtureId': fixture_id, 'jobKey': key,
             'updateQueue': str(queue), 'jobSha256': job_sha,
             'jobFile': [job_info.st_dev, job_info.st_ino], 'status': status}
    return {**proof, 'sha256': hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}


def _locks(runtime_root, update_queue, key):
    """Drain order, both non-blocking: UID dispatch lease, then per-job worker lock.

    ``drain`` holds the lease and then the update handler takes the job lock;
    the detached SessionStart worker takes only the job lock. Never waiting
    means no ordering can deadlock; a held lock is a refusal, not a queue.
    """
    from runtime_lease import coordination_lock
    stack = ExitStack()
    try:
        for path, busy in ((Path(runtime_root) / 'runtime-worker.lock', 'dispatch_busy'),
                           (Path(update_queue) / (key + '.lock'), 'update_worker_running')):
            try:
                fd = coordination_lock(path)
            except (OSError, ValueError) as exc:
                raise CancelRefused('unsafe_lock') from exc
            stack.callback(os.close, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CancelRefused(busy) from exc
    except BaseException:
        stack.close()
        raise
    return stack


def _audits(db, key):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_cancellations'").fetchone():
        return []
    return [dict(r) for r in db.execute(
        'SELECT * FROM wake_cancellations WHERE wake_id=? AND kind=? ORDER BY seq', (key, KIND))]


def status(project_raw, *, queue=None):
    """Read-only: the update wake row for this project and its cancel audit."""
    if not isinstance(project_raw, str) or not os.path.isabs(project_raw):
        return {'status': 'refused', 'reason': 'invalid_project'}
    from runtime_work import WakeQueue
    queue = queue or WakeQueue()
    target = job_key(project_raw)
    key = wake_policy.wake_key(KIND, target)
    if not queue.path.exists():
        return {'jobKey': target, 'wake': None, 'overflow': False, 'cancellations': []}
    db = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        db.row_factory = sqlite3.Row
        overflow = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_overflow'").fetchone()
                        and db.execute('SELECT 1 FROM wake_overflow WHERE id=?', (key,)).fetchone())
        return {'jobKey': target, 'wake': _row(db, key), 'overflow': overflow,
                'cancellations': _audits(db, key)}
    finally:
        db.close()


def cancel(project_raw, revision, backup_dir, *, queue=None, clock=time.time):
    """``applied`` / ``unchanged`` (audited replay) / ``refused``.

    Ownership is proven, then re-proven under both locks, and once more inside
    the queue transaction before commit. The CAS matches the full row version
    the verified backup holds, so an attempt/due/reason drift or a newer
    revision is never deleted. A refusal commits nothing to the queue; only an
    already-written backup file may remain.
    """
    if type(revision) is not int or revision <= 0:
        return {'status': 'refused', 'reason': 'invalid_revision'}
    try:
        from runtime_work import WakeQueue
        queue = queue or WakeQueue()
        first = ownership_proof(project_raw)
        with _locks(queue.root, first['updateQueue'], first['jobKey']):
            proof = ownership_proof(project_raw)
            if proof['sha256'] != first['sha256']:
                _refuse('proof_changed')
            return _cancel_locked(project_raw, revision, backup_dir, proof, queue, clock)
    except CancelRefused as exc:
        return {'status': 'refused', 'reason': str(exc)}
    except sqlite3.OperationalError as exc:
        # Busy or locked: nothing committed; inspect with ``update-wake-status``.
        return {'status': 'refused', 'reason': 'queue_busy', 'detail': type(exc).__name__}
    except ValueError as exc:
        # Unsafe runtime queue root/file (WakeQueue/connect); nothing touched.
        return {'status': 'refused', 'reason': 'unsafe_runtime_state', 'detail': str(exc)}
    except OSError as exc:
        # A proof file vanished or is unreadable (e.g. no fixture MANIFEST): not owned.
        return {'status': 'refused', 'reason': 'proof_unreadable', 'detail': type(exc).__name__}


def _cancel_locked(project_raw, revision, backup_dir, proof, queue, clock):
    key = wake_policy.wake_key(KIND, proof['jobKey'])
    with queue.connect() as db:
        current = _row(db, key)
        audits = _audits(db, key)
    if current is None:
        # Idempotent only for this exact audited target/revision; any other
        # absence is unexplained and fails closed.
        done = [a for a in audits if a['target'] == proof['jobKey'] and a['revision'] == revision]
        if not done:
            _refuse('wake_absent_unaudited')
        return {'status': 'unchanged', 'reason': 'already_cancelled', 'audit': done[-1], 'proof': proof}
    if current['kind'] != KIND or current['target'] != proof['jobKey']:
        _refuse('wake_identity_mismatch')
    if current['revision'] != revision:
        _refuse('revision_changed')
    if current['status'] != 'pending':
        _refuse('wake_not_pending')
    try:
        backup = _backup(queue, backup_dir, key, current)
    except CancelRefused:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CancelRefused('backup_failed') from exc
    with queue.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(AUDIT_TABLE)
        removed = db.execute(
            "DELETE FROM wakes WHERE id=? AND kind=? AND target=? AND revision=? AND status='pending' "
            "AND due=? AND attempts=? AND reason IS ?",
            (key, KIND, proof['jobKey'], revision, current['due'], current['attempts'],
             current['reason'])).rowcount
        if removed != 1:
            db.rollback()
            _refuse('wake_changed_before_delete')
        # Job/status preserved byte-for-byte across the backup; else roll back.
        if ownership_proof(project_raw)['sha256'] != proof['sha256']:
            db.rollback()
            _refuse('proof_changed')
        overflow = db.execute('DELETE FROM wake_overflow WHERE id=? AND kind=? AND target=?',
                              (key, KIND, proof['jobKey'])).rowcount
        db.execute('INSERT INTO wake_cancellations (at,wake_id,kind,target,revision,attempts,reason,due,'
                   'overflow_removed,proof_sha256,backup_path,backup_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (clock(), key, KIND, proof['jobKey'], revision, current['attempts'], current['reason'],
                    current['due'], overflow, proof['sha256'], backup['path'], backup['sha256']))
    # The delete and audit have committed. A concurrent writer may briefly make
    # the optional status read busy; never misreport this applied change as a
    # refusal and invite an unsafe retry.
    try:
        after = status(proof['project'], queue=queue)
    except sqlite3.Error:
        after = None
    return {'status': 'applied', 'revision': revision, 'overflowRemoved': overflow,
            'removedRow': {name: current[name] for name in ROW_VERSION},
            'backup': {k: backup[k] for k in ('path', 'sha256', 'quickCheck')},
            'proof': proof, 'after': after}
