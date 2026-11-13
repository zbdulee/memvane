"""Plugin-owned registry: stable identity, scoped views, and CLI/app concurrency."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from memvane.learning.context_learning.storage_layout import (
    project_view, register_project, registry, root,
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
        'from memvane.learning.context_learning.storage_layout import register_project\n'
        'barrier=pathlib.Path(sys.argv[3])\n'
        'while not barrier.exists(): time.sleep(.001)\n'
        'print(register_project(sys.argv[1], display_name="Process project", home=sys.argv[2]))\n'
    )
    children = [subprocess.Popen(
        [sys.executable, '-c', program, str(project), str(home), str(barrier)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'PYTHONPATH': '.'}) for _ in range(4)]
    barrier.touch()
    output = []
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        output.append(stdout.strip())
    assert len(set(output)) == 1

