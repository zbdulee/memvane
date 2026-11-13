"""Explicit local storage; hook capture fails open, offline operations fail closed."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

from .contracts import SUPPORTED_VERSIONS, validate_request
from .compact_input import derive


# Every connection takes the write lock (BEGIN IMMEDIATE), so a concurrent
# opener must wait for the holder's commit. Offline and CLI work waits up to
# BUSY_TIMEOUT; the blocking prompt hook (capture) must not stall a prompt
# and keeps the short HOOK_BUSY_TIMEOUT, failing open instead.
BUSY_TIMEOUT = 5.0
HOOK_BUSY_TIMEOUT = 0.05


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@contextmanager
def database(state_dir, *, home=None, timeout=BUSY_TIMEOUT):
    root = Path(state_dir)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError('invalid_state_dir')
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_state_permissions')
    from .history_activation import writer_access
    with writer_access(root / 'learning.sqlite3', home=home) as db_path:
        with _database_at(db_path, timeout=timeout) as db:
            yield db


@contextmanager
def _database_at(db_path, *, timeout=BUSY_TIMEOUT):
    if db_path.is_symlink():
        raise ValueError('invalid_state_dir')
    if db_path.exists():
        stat = db_path.stat()
        if stat.st_uid != os.getuid() or stat.st_nlink != 1:
            raise ValueError('unsafe_database')
    with closing(sqlite3.connect(str(db_path), timeout=timeout)) as db, db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError('unknown_database_version')
        if version == 2:
            db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('CREATE TABLE IF NOT EXISTS samples (request_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS tombstones (request_id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE IF NOT EXISTS labels (request_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, review TEXT, PRIMARY KEY(request_id,candidate_id))')
        db.execute('CREATE TABLE IF NOT EXISTS manifests (version_id TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS manifest_cases (version_id TEXT, request_id TEXT, candidate_id TEXT, PRIMARY KEY(version_id,request_id,candidate_id))')
        db.execute('CREATE TABLE IF NOT EXISTS family_splits (family_hash TEXT PRIMARY KEY, split TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS compact_inputs (request_id TEXT NOT NULL, candidate_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(request_id,candidate_id))')
        if version == 0:
            db.execute('PRAGMA user_version=1')
        db.execute('BEGIN IMMEDIATE')
        yield db


def capture(sample: dict, *, enabled: bool = False, state_dir=None,
            compact_sources=None, home=None) -> str:
    if enabled is not True:
        return 'disabled'
    if state_dir is None:
        return 'missing_state_dir'
    try:
        if not isinstance(sample, dict) or type(sample.get('schema_version')) is not int or sample.get('schema_version') not in SUPPORTED_VERSIONS:
            return 'invalid_schema'
        validate_request(sample)
        if compact_sources is None: compact_sources = {}
        if not isinstance(compact_sources, dict) or not set(compact_sources) <= {c['candidate_id'] for c in sample['candidates']}:
            return 'invalid_compact_sources'
        compact_rows = []
        for candidate in sample['candidates']:
            cid = candidate['candidate_id']
            if cid in compact_sources:
                compact_rows.append((sample['request_id'], cid,
                    canonical(derive(compact_sources[cid], candidate['revision_sha256']))))
        with database(state_dir, home=home, timeout=HOOK_BUSY_TIMEOUT) as db:
            rid = sample['request_id']
            if db.execute('SELECT 1 FROM tombstones WHERE request_id=?', (rid,)).fetchone():
                return 'deleted_case'
            row = db.execute('SELECT body FROM samples WHERE request_id=?', (rid,)).fetchone()
            body = canonical(sample)
            if row is not None and canonical(json.loads(row[0])) != body:
                return 'request_conflict'
            for request_id, cid, compact_body in compact_rows:
                old = db.execute('SELECT body FROM compact_inputs WHERE request_id=? AND candidate_id=?',
                    (request_id, cid)).fetchone()
                if old is not None and old[0] != compact_body: return 'compact_source_conflict'
            if row is None:
                db.execute('INSERT INTO samples VALUES (?, ?, ?)', (rid, sample['schema_version'], body))
            from .history_activation import active_mapping
            mapping = active_mapping(db, home=home)
            if mapping is not None:
                project_id, project_root, _story_root = mapping
                permitted_story = None
                if sample.get('context_origin') == 'tool_edit':
                    found = db.execute(
                        'SELECT story_root FROM case_projects WHERE request_id=? '
                        'AND project_id=? AND project_root=?',
                        (rid, project_id, project_root)).fetchone()
                    permitted_story = found[0] if found else None
                existing = db.execute(
                    'SELECT project_id,project_root,story_root FROM sample_projects '
                    'WHERE request_id=?', (rid,)).fetchone()
                expected = (project_id, project_root, permitted_story)
                if existing is None:
                    db.execute('INSERT INTO sample_projects VALUES (?,?,?,?)',
                               (rid, *expected))
                elif existing != expected:
                    raise ValueError('history_sample_mapping_conflict')
            for request_id, cid, compact_body in compact_rows:
                old = db.execute('SELECT 1 FROM compact_inputs WHERE request_id=? AND candidate_id=?',
                    (request_id, cid)).fetchone()
                if old is None:
                    db.execute('INSERT INTO compact_inputs VALUES (?,?,?)', (request_id, cid, compact_body))
        return 'stored'
    except ValueError as exc:
        return 'invalid_state_dir' if str(exc) == 'invalid_state_dir' else 'storage_unavailable'
    except (OSError, sqlite3.Error, TypeError, KeyError, AttributeError):
        return 'storage_unavailable'
