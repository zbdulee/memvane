"""Read-only corpus fingerprint and immutable review-queue reconciliation.

Only the selected Laya index's active document IDs/hashes and search settings
are read. This does not read private source bodies, issue a search, or call a
teacher/model.
"""
from __future__ import annotations

import json

from .store import canonical, database

MAX_DOCUMENTS = 50000


def snapshot(project_root, config):
    """Return a complete active-index snapshot or None, never a guessed hash.

    Only a project on a selected Laya route has an index; it is fingerprinted
    from laya-db (``laya_learning.corpus_snapshot``). Anything else, or an
    unverifiable selected route, returns None.
    """
    try:
        from . import laya_learning
        bound = laya_learning.selected(project_root)
    except Exception:
        return None
    if bound is None:
        return None
    return laya_learning.corpus_snapshot(project_root, config, decision=bound[0])

def reconcile(state_dir, current):
    """Queue old candidate pools for human review; never rewrite their samples."""
    if not isinstance(current, dict) or not isinstance(current.get('fingerprint'), str):
        return {'status': 'corpus_unavailable'}
    with database(state_dir) as db:
        db.execute('CREATE TABLE IF NOT EXISTS corpus_snapshots '
                   '(fingerprint TEXT PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS corpus_state '
                   '(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS corpus_review_queue '
                   '(request_id TEXT NOT NULL, current_fingerprint TEXT NOT NULL, '
                   'prior_fingerprint TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, '
                   'PRIMARY KEY(request_id,current_fingerprint))')
        fingerprint = current['fingerprint']
        db.execute('INSERT OR IGNORE INTO corpus_snapshots VALUES (?,?)',
                   (fingerprint, canonical(current['material'])))
        previous = db.execute('SELECT fingerprint FROM corpus_state WHERE id=1').fetchone()
        db.execute('INSERT INTO corpus_state(id,fingerprint) VALUES (1,?) '
                   'ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint',
                   (fingerprint,))
        pending = 0
        if previous is None or previous[0] != fingerprint:
            for rid, body in db.execute('SELECT request_id,body FROM samples'):
                sample = json.loads(body)
                prior = sample.get('sampling', {}).get('index_revision', 'unavailable')
                if prior == fingerprint:
                    continue
                inserted = db.execute('INSERT OR IGNORE INTO corpus_review_queue VALUES (?,?,?,?,?)',
                                      (rid, fingerprint, prior, 'candidate_pool_changed', 'pending_review'))
                pending += inserted.rowcount
        return {'status': 'recorded', 'fingerprint': fingerprint,
                'changed': previous is not None and previous[0] != fingerprint,
                'queued': pending}


def current_evaluation_ready(state_dir, version_id, current):
    """Current-quality use requires every pinned sample from this corpus."""
    if current is None:
        return False
    reconcile(state_dir, current)
    with database(state_dir) as db:
        members = db.execute('SELECT DISTINCT request_id FROM manifest_cases '
                             'WHERE version_id=?', (version_id,)).fetchall()
        if not members:
            return False
        for (rid,) in members:
            row = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()
            if row is None:
                return False
            sample = json.loads(row[0])
            if sample.get('sampling', {}).get('index_revision') != current['fingerprint']:
                return False
        return True
