"""Read-only QMD corpus fingerprint and immutable review-queue reconciliation.

Only QMD's active document IDs/hashes and search settings are read. This does
not read private source bodies, issue a search, or call a teacher/model.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import qmd_route
import sqlite_read

from .contracts import digest
from .store import canonical, database

MAX_DOCUMENTS = 50000


def snapshot(project_root, config, *, qmd_paths=None):
    """Return a complete active-index snapshot or None, never a guessed hash.

    A project on a selected Laya route is fingerprinted from laya-db only
    (``laya_learning.corpus_snapshot``); an unverifiable selected route
    returns None and never reads QMD. An explicit ``qmd_paths`` is the
    legacy QMD recall caller.
    """
    if qmd_paths is None:
        try:
            from . import laya_learning
            bound = laya_learning.selected(project_root)
        except Exception:
            return None
        if bound is not None:
            return laya_learning.corpus_snapshot(project_root, config, decision=bound[0])
    try:
        root = Path(project_root).resolve(strict=True)
        if not root.is_dir() or not isinstance(config, dict):
            return None
        names = sorted(set(config.get('collections', [])))
        if not names or len(names) > 32 or any(not isinstance(n, str) or not n for n in names):
            return None
        route = qmd_paths if qmd_paths is not None else qmd_route.project_paths(root)
        if route.get('shared') is True:
            import qmd_shared_store
            if not set(names).issubset(qmd_shared_store.owned_collections(root, config)):
                return None
        db_path = Path(route['INDEX_PATH'])
        if db_path is None or not db_path.is_file() or db_path.is_symlink():
            return None
        with sqlite_read.connect(db_path, timeout=.2) as db:
            placeholders = ','.join('?' for _ in names)
            cursor = db.execute(
                f'SELECT collection,path,hash FROM documents WHERE active=1 '
                f'AND collection IN ({placeholders}) ORDER BY collection,path', names)
            docs = cursor.fetchmany(MAX_DOCUMENTS + 1)
        if len(docs) > MAX_DOCUMENTS or any(
                not isinstance(c, str) or not isinstance(p, str) or not isinstance(h, str)
                or len(h) != 64 for c, p, h in docs):
            return None
        qmd_settings = Path(route['QMD_CONFIG_DIR']) / 'index.yml'
        if not qmd_settings.is_file() or qmd_settings.is_symlink() or qmd_settings.stat().st_size > 65536:
            return None
        qmd_config_sha = hashlib.sha256(qmd_settings.read_bytes()).hexdigest()
        policy = {key: config.get(key) for key in (
            'collections', 'collectionPaths', 'collectionRoles', 'recallStrategy',
            'minScore', 'rawFallbackMinScore', 'skipPaths', 'lexicalPatterns',
            'topN', 'excludeStatusesFromRecall', 'recallVerifiedOnly',
            'contextLearning')}
        material = {'schema': 'qmd-corpus-snapshot-v1', 'projectRoot': str(root),
                    'policy': policy, 'qmdConfigSha256': qmd_config_sha,
                    'runtimeGeneration': route.get('generation'),
                    'activeDocuments': [list(row) for row in docs]}
        return {'fingerprint': digest(canonical(material)), 'material': material,
                'activeDocuments': len(docs)}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


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
