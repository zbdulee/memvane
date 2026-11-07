"""Plugin-owned registry: stable identity, scoped views, and CLI/app concurrency."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from context_learning.storage_layout import (
    project_view, register_project, registry, relocate_project, root,
)


with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    project = home / 'workspace' / 'novel-one'
    story = project / 'allowed-story'
    story.mkdir(parents=True)
    other = home / 'workspace' / 'novel-two'
    other.mkdir()
    project_id = register_project(project, story_root=story,
                                  display_name='Novel One', home=home)
    other_id = register_project(other, display_name='Novel Two', home=home)
    assert project_id != other_id
    assert str(uuid.UUID(project_id)) == project_id
    assert register_project(project, story_root=story,
                            display_name='Novel One', home=home) == project_id
    assert root(home).stat().st_mode & 0o077 == 0
    assert (root(home) / 'registry.sqlite3').stat().st_mode & 0o077 == 0
    with registry(home) as cli, registry(home) as app:
        cli.execute('BEGIN IMMEDIATE')
        cli.execute('INSERT INTO run_quality VALUES (?,?,?,?,?,?)',
                    (project_id, 'run-1', 'tool-edit-8192-choice-v1', 'model-1',
                     json.dumps({'selection_seconds': .42}), 123.0))
        # A reader can inspect its own committed view while the CLI writes.
        assert app.execute('SELECT count(*) FROM run_quality').fetchone()[0] == 0
        cli.commit()
        assert app.execute('SELECT count(*) FROM run_quality WHERE project_id=?',
                           (project_id,)).fetchone()[0] == 1
        assert app.execute('SELECT count(*) FROM run_quality WHERE project_id=?',
                           (other_id,)).fetchone()[0] == 0
    moved = home / 'workspace' / 'renamed-novel'
    project.rename(moved)
    new_story = relocate_project(project_id, project, moved, home=home)
    assert new_story == str(moved / 'allowed-story')
    assert project_view(project_id, home=home)['current_root'] == str(moved)
    with registry(home) as db:
        assert db.execute('SELECT role FROM project_paths WHERE path=?',
                          (str(project),)).fetchone()[0] == 'former'
        assert db.execute('SELECT project_id FROM project_paths WHERE path=?',
                          (str(moved),)).fetchone()[0] == project_id
        assert db.execute('SELECT count(*) FROM run_quality WHERE project_id=?',
                          (project_id,)).fetchone()[0] == 1
        settings = json.loads(db.execute('SELECT body FROM project_settings WHERE project_id=?',
                                         (project_id,)).fetchone()[0])
        assert settings['story_root'] == str(moved / 'allowed-story')
    try:
        register_project(project, display_name='Reuse forbidden', home=home)
    except ValueError as exc:
        assert str(exc) == 'missing_project_path'
    else:
        raise AssertionError('former path accepted as new project')

with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-link-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    (home / 'Library').symlink_to(home / 'elsewhere', target_is_directory=True)
    project = home / 'project'
    project.mkdir()
    try:
        register_project(project, display_name='Blocked', home=home)
    except ValueError as exc:
        assert str(exc) == 'unsafe_learning_root'
    else:
        raise AssertionError('symlinked plugin root accepted')

with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-race-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    project = home / 'project'
    project.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: register_project(
            project, display_name='Concurrent project', home=home), range(8)))
    assert len(set(ids)) == 1
    assert project_view(ids[0], home=home)['current_root'] == str(project)
    with registry(home) as cli:
        cli.execute('BEGIN IMMEDIATE')
        cli.execute('INSERT INTO run_quality VALUES (?,?,?,?,?,?)',
                    (ids[0], 'uncommitted', 'view', 'model', '{}', 124.0))
        started = time.monotonic()
        with registry(home) as app:
            assert app.execute('SELECT count(*) FROM run_quality').fetchone()[0] == 0
        assert time.monotonic() - started < 2.5
        cli.rollback()

with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-process-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    project = home / 'project'
    project.mkdir()
    barrier = home / 'go'
    program = (
        'import pathlib,sys,time\n'
        'from context_learning.storage_layout import register_project\n'
        'barrier=pathlib.Path(sys.argv[3])\n'
        'while not barrier.exists(): time.sleep(.001)\n'
        'print(register_project(sys.argv[1], display_name="Process project", home=sys.argv[2]))\n'
    )
    children = [subprocess.Popen(
        [sys.executable, '-c', program, str(project), str(home), str(barrier)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'PYTHONPATH': 'core'}) for _ in range(4)]
    barrier.touch()
    output = []
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        output.append(stdout.strip())
    assert len(set(output)) == 1

with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-escape-', dir='/tmp') as directory:
    home = Path(directory).resolve()
    project = home / 'project'
    story = project / 'sub' / 'story'
    story.mkdir(parents=True)
    project_id = register_project(project, story_root=story,
                                  display_name='Nested story', home=home)
    moved = home / 'moved'
    project.rename(moved)
    outside = home / 'outside'
    shutil.move(str(moved / 'sub'), str(outside))
    (moved / 'sub').symlink_to(outside, target_is_directory=True)
    try:
        relocate_project(project_id, project, moved, home=home)
    except ValueError as exc:
        assert str(exc) == 'story_relocation_unverified'
    else:
        raise AssertionError('nested story symlink escape accepted')
    assert project_view(project_id, home=home)['current_root'] == str(project)


# A registry last closed by a stock SQLite build is a WAL file without -wal or
# -shm. Apple's /usr/lib/libsqlite3 (the macOS system python3) refuses mode=ro
# there, which made root() pass over the MemVane registry for the legacy one.
with tempfile.TemporaryDirectory(prefix='qmd-learning-layout-cold-', dir='/tmp') as directory:
    import sqlite3
    from contextlib import closing
    from context_learning.storage_layout import LEGACY_ROOT_PARTS, ROOT_PARTS, SCHEMA_VERSION
    home = Path(directory).resolve()
    current, legacy = home.joinpath(*ROOT_PARTS), home.joinpath(*LEGACY_ROOT_PARTS)
    for base, wal in ((current, True), (legacy, False)):
        base.mkdir(mode=0o700, parents=True)
        path = base / 'registry.sqlite3'
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            if wal:
                assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            db.execute('CREATE TABLE projects (project_id TEXT PRIMARY KEY)')
            if wal:
                assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        path.chmod(0o600)
    cold = current / 'registry.sqlite3'
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(cold) + suffix)
        if sidecar.exists():
            assert suffix != '-wal' or sidecar.stat().st_size == 0
            sidecar.unlink()
    assert tuple(cold.read_bytes()[18:20]) == (2, 2)
    assert [p.name for p in current.iterdir()] == ['registry.sqlite3']
    assert root(home) == current, root(home)
