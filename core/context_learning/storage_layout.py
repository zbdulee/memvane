"""Plugin-owned learning registry for CLI and future local app readers.

This registry is deliberately separate from the legacy experiment database.
Registering a project cannot move its captures, change consent, or authorize a
model call. A later verified migration will switch the canonical history.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


SCHEMA_VERSION = 1
ROOT_PARTS = ('Library', 'Application Support', 'memvane', 'learning')
LEGACY_ROOT_PARTS = ('Library', 'Application Support', 'qmd-auto-context', 'learning')


def root(home=None):
    """The registry root; a registry created before the rename stays in place."""
    base = Path(home or Path.home())
    current, legacy = base.joinpath(*ROOT_PARTS), base.joinpath(*LEGACY_ROOT_PARTS)
    if not current.exists() and legacy.exists():
        return legacy
    return current


def _private_directory(path, *, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('unsafe_learning_directory')
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe_learning_directory')
    return path


def _validated_root(home=None, *, create=False):
    base = root(home)
    user_home = Path.home() if home is None else Path(home)
    if not base.is_absolute() or base == user_home or user_home not in base.parents:
        raise ValueError('unsafe_learning_root')
    if any(part.is_symlink() for part in (base, *base.parents) if part != user_home
           and user_home in part.parents):
        raise ValueError('unsafe_learning_root')
    return _private_directory(base, create=create)


def _absolute_existing_directory(value):
    path = Path(value)
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('unsafe_project_path')
    if not path.is_dir():
        raise ValueError('missing_project_path')
    return str(path.resolve(strict=True))


def _bounded_label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or '\x00' in value:
        raise ValueError('invalid_project_label')
    return value


@contextmanager
def registry(home=None, *, create=False):
    """Open the shared registry with bounded SQLite writer contention."""
    base = _validated_root(home, create=create)
    path = base / 'registry.sqlite3'
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    if path.is_symlink():
        raise ValueError('unsafe_registry')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('unsafe_registry')
    elif not create:
        raise FileNotFoundError(path)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA foreign_keys=ON')
        # A read-side opener must not request a write lock: app readers need
        # the last committed WAL view while a CLI writer is in a transaction.
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            # First-use DDL and journal-mode changes need a process lock.
            # SQLite's busy timeout does not reliably serialize WAL PRAGMA.
            lock_path = base / 'registry-init.lock'
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                lock_info = os.fstat(lock_fd)
                if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                        or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077):
                    raise ValueError('unsafe_registry_lock')
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ValueError('registry_init_busy')
                        time.sleep(.02)
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    if not create:
                        raise ValueError('uninitialized_registry')
                    db.execute('PRAGMA journal_mode=WAL')
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('CREATE TABLE projects (project_id TEXT PRIMARY KEY, current_root TEXT NOT NULL UNIQUE, story_root TEXT, display_name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)')
                        db.execute('CREATE TABLE project_paths (path TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id), role TEXT NOT NULL CHECK(role IN (\'current\',\'former\')))')
                        db.execute('CREATE INDEX project_paths_project ON project_paths(project_id,role)')
                        db.execute('CREATE TABLE project_settings (project_id TEXT PRIMARY KEY REFERENCES projects(project_id), schema_version INTEGER NOT NULL, body TEXT NOT NULL, updated_at REAL NOT NULL)')
                        db.execute('CREATE TABLE run_quality (project_id TEXT NOT NULL REFERENCES projects(project_id), run_id TEXT NOT NULL, input_view TEXT NOT NULL, model_version TEXT NOT NULL, body TEXT NOT NULL, recorded_at REAL NOT NULL, PRIMARY KEY(project_id,run_id))')
                        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                    version = SCHEMA_VERSION
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        if version != SCHEMA_VERSION:
            raise ValueError('unknown_registry_version')
        yield db
    finally:
        db.close()


def register_project(project_root, *, story_root=None, display_name, home=None):
    project = _absolute_existing_directory(project_root)
    story = _absolute_existing_directory(story_root) if story_root is not None else None
    if story is not None and Path(project) not in Path(story).parents:
        raise ValueError('story_outside_project')
    label = _bounded_label(display_name)
    with registry(home, create=True) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT project_id,story_root FROM projects WHERE current_root=?', (project,)).fetchone()
        if row is not None:
            if row[1] != story:
                raise ValueError('project_scope_changed')
            return row[0]
        if db.execute('SELECT 1 FROM project_paths WHERE path=?', (project,)).fetchone():
            raise ValueError('former_project_path_requires_relocation')
        project_id = str(uuid.uuid4())
        now = time.time()
        db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)', (project_id, project, story, label, now, now))
        db.execute('INSERT INTO project_paths VALUES (?,?,?)', (project, project_id, 'current'))
        db.execute('INSERT INTO project_settings VALUES (?,?,?,?)',
                   (project_id, SCHEMA_VERSION, json.dumps({'schema_version': SCHEMA_VERSION,
                    'story_root': story, 'external_data_scope': 'separate-owner-consent'},
                    sort_keys=True, separators=(',', ':')), now))
        return project_id


def relocate_project(project_id, old_root, new_root, *, home=None):
    """Explicitly preserve identity; never infer relocation from a folder name."""
    uuid.UUID(project_id)
    old = str(Path(old_root).resolve(strict=False))
    new = _absolute_existing_directory(new_root)
    with registry(home) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT current_root,story_root FROM projects WHERE project_id=?', (project_id,)).fetchone()
        if row is None or row[0] != old or old == new:
            raise ValueError('project_relocation_mismatch')
        if db.execute('SELECT 1 FROM project_paths WHERE path=?', (new,)).fetchone():
            raise ValueError('project_path_already_registered')
        story = row[1]
        if story is not None:
            relative = Path(story).relative_to(old)
            try:
                moved_story = _absolute_existing_directory(Path(new) / relative)
            except ValueError as exc:
                raise ValueError('story_relocation_unverified') from exc
            if Path(new) not in Path(moved_story).parents:
                raise ValueError('story_relocation_unverified')
            story = moved_story
        now = time.time()
        db.execute('UPDATE projects SET current_root=?,story_root=?,updated_at=? WHERE project_id=?',
                   (new, story, now, project_id))
        db.execute('UPDATE project_paths SET role=\'former\' WHERE path=? AND project_id=?', (old, project_id))
        db.execute('INSERT INTO project_paths VALUES (?,?,?)', (new, project_id, 'current'))
        settings = {'schema_version': SCHEMA_VERSION, 'story_root': story,
                    'external_data_scope': 'separate-owner-consent'}
        db.execute('UPDATE project_settings SET body=?,updated_at=? WHERE project_id=?',
                   (json.dumps(settings, sort_keys=True, separators=(',', ':')), now, project_id))
        return story


def project_view(project_id, *, home=None):
    uuid.UUID(project_id)
    with registry(home) as db:
        row = db.execute('SELECT project_id,current_root,story_root,display_name FROM projects WHERE project_id=?', (project_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(('project_id', 'current_root', 'story_root', 'display_name'), row))
