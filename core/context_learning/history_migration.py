"""Prepare and verify one plugin-owned, single-file learning-history snapshot.

Preparation never changes the active store or invokes a teacher. The legacy
databases remain the recoverable originals until every installed host can use
one atomic active pointer. An uncertain invocation is copied, never retried.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid

from .storage_layout import project_view, root as learning_root
from .store import canonical


SCHEMA = 'qmd-plugin-learning-migration-v1'
HISTORY_VERSION = 2


def _regular_owner_file(path):
    path = Path(path)
    if (not path.is_absolute() or any(part.is_symlink() for part in
            (path, *path.parents)) or not path.is_file()):
        raise ValueError('unsafe_legacy_database')
    info = path.stat()
    parent = path.parent
    parent_info = parent.stat()
    if (parent.is_symlink() or parent_info.st_uid != os.getuid()
            or parent_info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise ValueError('unsafe_legacy_database')
    return path.resolve(strict=True)


def _read(path):
    # WAL-mode SQLite may need to recreate a missing -shm sidecar. Open the
    # owner file in rw mode for that metadata, then prohibit SQL writes.
    db = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    return db


def _source_binding(view, shared_path, private_path):
    from .contracts import digest
    project = Path(view['current_root'])
    settings = project / '.auto-context/settings.json'
    if settings.is_symlink() or not settings.is_file():
        raise ValueError('project_settings_missing')
    raw = settings.read_bytes()
    cfg = json.loads(raw)
    learning = cfg.get('contextLearning') if isinstance(cfg, dict) else None
    shared = cfg.get('sharedLearning') if isinstance(cfg, dict) else None
    if (not isinstance(learning, dict) or learning.get('capture') is not True
            or not isinstance(shared, dict) or shared.get('enabled') is not True
            or shared.get('root') != str(shared_path.parent)):
        raise ValueError('project_history_binding_changed')
    parent = learning.get('stateRoot')
    if not isinstance(parent, str) or not Path(parent).is_absolute():
        raise ValueError('project_history_binding_changed')
    parent = Path(parent)
    if (not parent.is_dir() or any(part.is_symlink() for part in (parent, *parent.parents))
            or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077
            or private_path != parent / digest(str(project)) / 'learning.sqlite3'):
        raise ValueError('project_history_binding_changed')
    return hashlib.sha256(raw).hexdigest()


def _tables(db):
    return {name: sql for name, sql in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _digest_value(value):
    # SQLite values are None/int/float/str/bytes. JSON has no bytes, and card
    # registrations store exact card bytes as BLOBs. A dict marker cannot
    # collide with any SQLite value, and non-BLOB rows keep the original
    # digest so manifests prepared before BLOB support stay verifiable.
    if isinstance(value, bytes):
        return {'$blob': value.hex()}
    return value


def _table_digest(db, table):
    if not table.replace('_', '').isalnum():
        raise ValueError('invalid_table_name')
    rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
    digest = hashlib.sha256()
    for row in sorted(canonical([_digest_value(value) for value in row]) for row in rows):
        digest.update(row.encode('utf8'))
        digest.update(b'\n')
    return {'rows': len(rows), 'sha256': digest.hexdigest()}


def _inventory(db):
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('legacy_database_corrupt')
    return {name: _table_digest(db, name) for name in sorted(_tables(db))}


def _new_file(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_migration_directory')
    return path


def _write_manifest(path, value):
    raw = (canonical(value) + '\n').encode('utf8')
    _new_file(path)
    with open(path, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return hashlib.sha256(raw).hexdigest()


def prepare(project_id, shared_path, private_path, *, home=None):
    """Stage a consistent merged copy; no active pointer or legacy write."""
    view = project_view(project_id, home=home)
    if view is None:
        raise ValueError('unregistered_project')
    shared_path = _regular_owner_file(shared_path)
    private_path = _regular_owner_file(private_path)
    if shared_path == private_path:
        raise ValueError('legacy_databases_overlap')
    settings_sha256 = _source_binding(view, shared_path, private_path)
    base = learning_root(home)
    if not base.is_dir() or base.is_symlink() or base.stat().st_mode & 0o077:
        raise ValueError('unsafe_learning_root')
    migrations = base / 'migrations'
    if not migrations.exists():
        migrations.mkdir(mode=0o700)
    if migrations.is_symlink() or not migrations.is_dir() or migrations.stat().st_mode & 0o077:
        raise ValueError('unsafe_migration_directory')
    generation = 'g-' + uuid.uuid4().hex
    staged = _private_dir(migrations / generation)
    target = staged / 'history.sqlite3'
    private_snapshot = staged / 'private-snapshot.sqlite3'
    try:
        _new_file(target)
        _new_file(private_snapshot)
        with closing(_read(shared_path)) as shared, closing(sqlite3.connect(target)) as history:
            shared.backup(history)
            # A migrated history is one sealed file. The source may use WAL;
            # force the copied destination back to rollback journaling.
            if history.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise ValueError('history_journal_mode_not_sealed')
        with closing(_read(private_path)) as private, closing(sqlite3.connect(private_snapshot)) as snapshot:
            private.backup(snapshot)
        with closing(_read(target)) as shared_copy, closing(_read(private_snapshot)) as private_copy:
            shared_inventory = _inventory(shared_copy)
            private_inventory = _inventory(private_copy)
            overlap = set(shared_inventory) & set(private_inventory)
            if overlap:
                raise ValueError('history_table_collision:' + ','.join(sorted(overlap)))
            # This migration is deliberately one project at a time. It must
            # not accidentally tag another project's cases as this story.
            if any(row[0] != view['current_root'] for row in shared_copy.execute(
                    'SELECT DISTINCT project_root FROM cases')):
                raise ValueError('shared_history_contains_other_projects')
            historical_key = hashlib.sha256(view['current_root'].encode('utf8')).hexdigest()
            if any(row[0] != historical_key for row in shared_copy.execute(
                    'SELECT DISTINCT project_key FROM cases')):
                raise ValueError('shared_history_project_key_mismatch')
            if any(row[0] != view['current_root'] or row[1] != view['story_root']
                   for row in shared_copy.execute('SELECT DISTINCT project_root,story_root FROM edit_tickets')):
                raise ValueError('edit_ticket_scope_mismatch')
            # Card registrations carry their own scope, and their readers
            # (latest_paths) do not filter by project. Foreign rows would
            # silently join this project's provenance census.
            shared_tables = _tables(shared_copy)
            for table in ('source_card_versions', 'topical_worker_versions'):
                if table in shared_tables and any(
                        row[0] != view['current_root'] or row[1] != view['story_root']
                        for row in shared_copy.execute(
                            f'SELECT DISTINCT project_root,story_root FROM "{table}"')):
                    raise ValueError('card_registration_scope_mismatch')
            private_samples = dict(private_copy.execute('SELECT request_id,body FROM samples'))
            compact_inputs = {(request_id, candidate_id): json.loads(body)
                              for request_id, candidate_id, body in private_copy.execute(
                                  'SELECT request_id,candidate_id,body FROM compact_inputs')}
            tickets = {row[0]: row[1:] for row in shared_copy.execute(
                'SELECT request_id,ticket_id,input_sha256,candidate_sha256,segment_sha256,event_json '
                'FROM edit_tickets WHERE request_id IS NOT NULL')}
            verified_edit_ids = set()
            for request_id, shared_body, input_sha, bodies_json in shared_copy.execute(
                    'SELECT request_id,sample,input_sha256,bodies FROM cases'):
                local_body = private_samples.get(request_id)
                if (local_body is None or json.loads(local_body) != json.loads(shared_body)
                        or hashlib.sha256(shared_body.encode('utf8')).hexdigest() != input_sha):
                    raise ValueError('shared_private_capture_mismatch')
                sample = json.loads(shared_body)
                bodies = json.loads(bodies_json)
                candidates = sample.get('candidates')
                if (not isinstance(candidates, list) or not isinstance(bodies, dict)
                        or set(bodies) != {row['candidate_id'] for row in candidates}):
                    raise ValueError('shared_candidate_snapshot_mismatch')
                for candidate in candidates:
                    source = compact_inputs.get((request_id, candidate['candidate_id']))
                    body = bodies[candidate['candidate_id']]
                    if (not isinstance(source, dict) or not isinstance(body, str)
                            or source.get('body_text') != body
                            or source.get('body_sha256') != hashlib.sha256(body.encode('utf8')).hexdigest()
                            or source.get('source_sha256') != candidate['revision_sha256']):
                        raise ValueError('shared_candidate_snapshot_mismatch')
                if sample.get('context_origin') == 'tool_edit':
                    ticket = tickets.get(request_id)
                    candidate_sha = hashlib.sha256(canonical([
                        [row['candidate_id'], row['revision_sha256']]
                        for row in candidates]).encode('utf8')).hexdigest()
                    if (ticket is None or sample.get('edit_event', {}).get('ticket_id') != ticket[0]
                            or ticket[1] != input_sha or ticket[2] != candidate_sha
                            or sample.get('prompt', {}).get('sha256') != ticket[3]
                            or sample['edit_event'] != {**json.loads(ticket[4]), 'ticket_id': ticket[0]}):
                        raise ValueError('edit_ticket_capture_mismatch')
                    verified_edit_ids.add(request_id)
        with closing(sqlite3.connect(target)) as history, closing(_read(private_snapshot)) as private_copy, history:
            history.execute('PRAGMA foreign_keys=ON')
            history.execute('BEGIN IMMEDIATE')
            for table, ddl in _tables(private_copy).items():
                history.execute(ddl)
                rows = private_copy.execute(f'SELECT * FROM "{table}"').fetchall()
                if rows:
                    marks = ','.join('?' for _ in rows[0])
                    history.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
            for _name, ddl in private_copy.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
                history.execute(ddl)
            history.execute('CREATE TABLE case_projects (project_key TEXT NOT NULL, request_id TEXT NOT NULL, project_id TEXT NOT NULL, project_root TEXT NOT NULL, story_root TEXT, PRIMARY KEY(project_key,request_id), FOREIGN KEY(project_key,request_id) REFERENCES cases(project_key,request_id))')
            history.execute('CREATE INDEX case_projects_project ON case_projects(project_id,request_id)')
            history.execute('CREATE TABLE sample_projects (request_id TEXT PRIMARY KEY REFERENCES samples(request_id), project_id TEXT NOT NULL, project_root TEXT NOT NULL, story_root TEXT)')
            history.execute('CREATE INDEX sample_projects_project ON sample_projects(project_id,request_id)')
            history.execute('CREATE TABLE model_selections (request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, model_version TEXT NOT NULL, input_view TEXT NOT NULL, selected_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL)')
            history.execute('CREATE TABLE history_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            for project_key, request_id, sample in history.execute('SELECT project_key,request_id,sample FROM cases').fetchall():
                origin = json.loads(sample).get('context_origin')
                story = view['story_root'] if origin == 'tool_edit' else None
                history.execute('INSERT INTO case_projects VALUES (?,?,?,?,?)',
                                (project_key, request_id, project_id, view['current_root'], story))
            for request_id, sample in history.execute('SELECT request_id,body FROM samples').fetchall():
                origin = json.loads(sample).get('context_origin')
                # Private capture can contain edits never admitted to the
                # synthetic shared queue. Only a validated shared ticket
                # establishes this one story's external-data scope.
                story = view['story_root'] if origin == 'tool_edit' and request_id in verified_edit_ids else None
                history.execute('INSERT INTO sample_projects VALUES (?,?,?,?)',
                                (request_id, project_id, view['current_root'], story))
            history.execute('INSERT INTO history_metadata VALUES (?,?)', ('schema', SCHEMA))
            history.execute('INSERT INTO history_metadata VALUES (?,?)', ('generation', generation))
            history.execute(f'PRAGMA user_version={HISTORY_VERSION}')
        with closing(sqlite3.connect(target)) as history:
            checkpoint = history.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if (checkpoint is None or checkpoint[0] != 0
                    or checkpoint[1] >= 0 and checkpoint[1] != checkpoint[2]):
                raise ValueError('history_checkpoint_incomplete')
        wal_path = Path(str(target) + '-wal')
        if wal_path.exists() and wal_path.stat().st_size:
            raise ValueError('history_wal_not_empty')
        with closing(_read(target)) as combined:
            if combined.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ValueError('history_journal_mode_not_sealed')
            history_inventory = _inventory(combined)
            if any(_table_digest(combined, name) != value for name, value in shared_inventory.items()):
                raise ValueError('shared_history_copy_mismatch')
            if any(_table_digest(combined, name) != value for name, value in private_inventory.items()):
                raise ValueError('private_history_copy_mismatch')
            if combined.execute('SELECT count(*) FROM case_projects').fetchone()[0] != shared_inventory['cases']['rows']:
                raise ValueError('case_project_mapping_incomplete')
            if combined.execute('SELECT count(*) FROM sample_projects').fetchone()[0] != private_inventory['samples']['rows']:
                raise ValueError('sample_project_mapping_incomplete')
            reservation_records = combined.execute("SELECT count(*) FROM experiment_calls WHERE call_key LIKE 'gold:%:first_running' OR call_key LIKE 'gold:%:second_running'").fetchone()[0]
            unresolved = sorted({(row[0], row[1]) for row in combined.execute(
                "SELECT c.request_id, CASE WHEN e.call_key='gold:'||c.request_id||':first_running' "
                "THEN 'first' ELSE 'second' END FROM cases c JOIN experiment_calls e "
                "ON e.call_key IN ('gold:'||c.request_id||':first_running', "
                "'gold:'||c.request_id||':second_running') WHERE c.gold IS NULL "
                "AND ((e.call_key='gold:'||c.request_id||':first_running' AND c.first IS NULL) "
                "OR (e.call_key='gold:'||c.request_id||':second_running' AND c.second IS NULL))")})
            pending_first = sum(1 for row in combined.execute(
                "SELECT 1 FROM cases c JOIN experiment_calls e "
                "ON e.call_key='gold:'||c.request_id||':first_running' "
                "WHERE c.status='pending_review' AND c.first IS NULL AND c.gold IS NULL"))
        private_snapshot.unlink()
        manifest = {'schema': SCHEMA, 'history_version': HISTORY_VERSION,
                    'generation': generation, 'project_id': project_id,
                    'project_root': view['current_root'], 'story_root': view['story_root'],
                    'project_settings_sha256': settings_sha256,
                    'source_shared': str(shared_path), 'source_private': str(private_path),
                    'shared_tables': shared_inventory, 'private_tables': private_inventory,
                    'history_tables': history_inventory,
                    'gold_call_reservation_records': reservation_records,
                    'unresolved_gold_phases': len(unresolved),
                    'pending_review_unanswered_first': pending_first,
                    'unresolved_phase_ids_sha256': hashlib.sha256(
                        canonical(unresolved).encode('utf8')).hexdigest(),
                    'history_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                    'active': False}
        _write_manifest(staged / 'prepared.json', manifest)
        return {'generation': generation, 'path': str(target),
                'shared_cases': shared_inventory['cases']['rows'],
                'private_samples': private_inventory['samples']['rows'],
                'gold_call_reservation_records': reservation_records,
                'unresolved_gold_phases': len(unresolved),
                'pending_review_unanswered_first': pending_first, 'active': False}
    except Exception:
        shutil.rmtree(staged)
        raise


def verify_prepared(generation, *, home=None):
    """Verify staged data and source freshness before a later switch."""
    if not isinstance(generation, str) or not re.fullmatch(r'g-[0-9a-f]{32}', generation):
        raise ValueError('invalid_generation')
    migrations = learning_root(home) / 'migrations'
    staged = migrations / generation
    if staged.is_symlink() or staged.resolve(strict=True).parent != migrations.resolve(strict=True):
        raise ValueError('invalid_generation')
    manifest_path = _regular_owner_file(staged / 'prepared.json')
    value = json.loads(manifest_path.read_text())
    if value.get('schema') != SCHEMA or value.get('generation') != generation or value.get('active') is not False:
        raise ValueError('invalid_migration_manifest')
    target = _regular_owner_file(staged / 'history.sqlite3')
    wal_path = Path(str(target) + '-wal')
    if wal_path.exists() and wal_path.stat().st_size:
        raise ValueError('prepared_history_wal_not_empty')
    if hashlib.sha256(target.read_bytes()).hexdigest() != value['history_sha256']:
        raise ValueError('prepared_history_changed')
    with closing(_read(target)) as combined:
        if combined.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('prepared_history_journal_mode_changed')
        if _inventory(combined) != value['history_tables']:
            raise ValueError('prepared_history_table_changed')
    if wal_path.exists() and wal_path.stat().st_size:
        raise ValueError('prepared_history_wal_not_empty')
    sources_current = True
    view = project_view(value['project_id'], home=home)
    if view is None or view['current_root'] != value['project_root'] or view['story_root'] != value['story_root']:
        sources_current = False
    else:
        try:
            sources_current = _source_binding(view, Path(value['source_shared']),
                                             Path(value['source_private'])) == value['project_settings_sha256']
        except (OSError, ValueError, TypeError, KeyError):
            sources_current = False
    for name, inventory in [('source_shared', value['shared_tables']),
                            ('source_private', value['private_tables'])]:
        with closing(_read(_regular_owner_file(value[name]))) as db:
            if _inventory(db) != inventory:
                sources_current = False
    return {'generation': generation, 'history_verified': True,
            'sources_current': sources_current, 'active': False}
