"""Read-only wake row status (``install.runtime_wake_cancel``).

``status`` reads one wake row, its overflow flag and the audit rows a queue
may hold in ``wake_cancellations``.
"""
from __future__ import annotations

import sqlite3

from memvane.runtime import runtime_wake_policy as wake_policy


def _row(db, key):
    found = db.execute('SELECT * FROM wakes WHERE id=?', (key,)).fetchone()
    return dict(found) if found is not None else None


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

