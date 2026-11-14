"""Queue half of the owned synthetic-QA wake cancel (``install.runtime_wake_cancel``).

The caller proves ownership and passes a re-proof. This module reads the wake
row, takes and verifies an online SQLite backup, and in one transaction
CAS-deletes exactly (kind, target, revision, pending), removes a coalesced
overflow row and appends an audit row to ``wake_cancellations``.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import time

from memvane.runtime import runtime_wake_policy as wake_policy
from memvane.runtime.types import CANCEL_ROW_VERSION as ROW_VERSION, CancelRefused

AUDIT_TABLE = ('CREATE TABLE IF NOT EXISTS wake_cancellations (seq INTEGER PRIMARY KEY AUTOINCREMENT,'
               'at REAL NOT NULL,wake_id TEXT NOT NULL,kind TEXT NOT NULL,target TEXT NOT NULL,'
               'revision INTEGER NOT NULL,attempts INTEGER NOT NULL,reason TEXT,due REAL NOT NULL,'
               'overflow_removed INTEGER NOT NULL,proof_sha256 TEXT NOT NULL,'
               'backup_path TEXT NOT NULL,backup_sha256 TEXT NOT NULL)')


def _refuse(reason):
    raise CancelRefused(reason)


def _row(db, key):
    found = db.execute('SELECT * FROM wakes WHERE id=?', (key,)).fetchone()
    return dict(found) if found is not None else None


def _backup(queue, backup_dir, key, expected):
    """Online SQLite backup, verified to hold exactly the row about to be removed."""
    revision = expected['revision']
    directory = Path(backup_dir)
    if (not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents))
            or not directory.is_dir()):
        _refuse('unsafe_backup_dir')
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        _refuse('unsafe_backup_dir')
    target = directory / f'runtime-wakes-before-cancel-{revision}-{time.time_ns()}.sqlite3'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    source = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    check = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
    try:
        check.row_factory = sqlite3.Row
        ok = check.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        row = _row(check, key)
    finally:
        check.close()
    if not ok or row is None or any(row[name] != expected[name] for name in ROW_VERSION):
        _refuse('backup_unverified')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {'path': str(target), 'sha256': digest, 'quickCheck': 'ok', 'row': row}


def status(kind, target, *, queue=None):
    """Read-only: the current wake row for ``(kind, target)`` and its cancel audit."""
    from memvane.runtime.runtime_work import WakeQueue
    queue = queue or WakeQueue()
    key = wake_policy.wake_key(kind, target)
    if not queue.path.exists():
        return {'wake': None, 'cancellations': []}
    db = sqlite3.connect(queue.path.as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        db.row_factory = sqlite3.Row
        audits = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_cancellations'").fetchone():
            audits = [dict(r) for r in db.execute(
                'SELECT * FROM wake_cancellations WHERE wake_id=? ORDER BY seq', (key,))]
        overflow = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_overflow'").fetchone()
                        and db.execute('SELECT 1 FROM wake_overflow WHERE id=?', (key,)).fetchone())
        return {'wake': _row(db, key), 'overflow': overflow, 'cancellations': audits}
    finally:
        db.close()


def cancel_pending(kind, target, revision, backup_dir, proof_sha256, still_owned, *, queue=None, clock=time.time):
    """CAS-delete one pending wake row version; ``None`` when no row exists.

    ``still_owned()`` re-proves ownership inside the transaction after the
    DELETE; ``False`` rolls back. Returns ``(row, backup, overflow_removed)``.
    Raises ``CancelRefused``/``sqlite3.OperationalError`` before any commit.
    """
    from memvane.runtime.runtime_work import WakeQueue
    queue = queue or WakeQueue()
    key = wake_policy.wake_key(kind, target)
    with queue.connect() as db:
        current = _row(db, key)
    if current is None:
        return None
    if current['kind'] != kind or current['target'] != target:
        _refuse('wake_identity_mismatch')
    if current['revision'] != revision:
        _refuse('revision_changed')
    if current['status'] != 'pending':
        _refuse('wake_not_pending')
    backup = _backup(queue, backup_dir, key, current)
    with queue.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(AUDIT_TABLE)
        removed = db.execute(
            "DELETE FROM wakes WHERE id=? AND kind=? AND target=? AND revision=? AND status='pending' "
            "AND due=? AND attempts=? AND reason IS ?",
            (key, kind, target, revision, current['due'], current['attempts'],
             current['reason'])).rowcount
        if removed != 1:
            db.rollback()
            _refuse('wake_changed_before_delete')
        if not still_owned():
            db.rollback()
            _refuse('proof_changed')
        overflow = db.execute('DELETE FROM wake_overflow WHERE id=?', (key,)).rowcount
        db.execute('INSERT INTO wake_cancellations (at,wake_id,kind,target,revision,attempts,reason,due,'
                   'overflow_removed,proof_sha256,backup_path,backup_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (clock(), key, kind, target, revision, current['attempts'], current['reason'],
                    current['due'], overflow, proof_sha256, backup['path'], backup['sha256']))
    return current, backup, overflow
