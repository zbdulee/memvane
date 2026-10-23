"""Host hook/queue handoff on one isolated shared index, without QMD/model calls."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path.cwd() / 'core'))
import config
import dirty_queue
import qmd_route
import qmd_shared_store
import resolve_paths
import runtime_work
import sync
import compile_paths
import update_hook_queue

with tempfile.TemporaryDirectory(prefix='qmd-shared-start-', dir=Path.cwd().parent) as temp:
    home = Path(temp).resolve()
    base = home / 'novel'
    base.mkdir()
    (base / '.auto-context').mkdir()
    (base / '.auto-context/settings.json').write_text(json.dumps(
        {'indexing': False, 'ownedScopes': ['story-a', 'story-b']}))
    stories = [base / 'story-a', base / 'story-b']
    for i, story in enumerate(stories):
        (story / 'wiki').mkdir(parents=True)
        (story / 'wiki/card.md').write_text(f'Own synthetic card {i}.\n')
        (story / '.auto-context').mkdir()
        (story / '.auto-context/settings.json').write_text(json.dumps({
            'indexing': True, 'collections': [f'story-{i}-wiki'],
            'collectionPaths': {f'story-{i}-wiki': 'wiki'},
            'collectionRoles': {f'story-{i}-wiki': 'wiki'}}))
    prepared = subprocess.run([sys.executable,
        'test/fixtures/prepare-shared-qmd-store.py', str(home)],
        cwd=Path.cwd(), capture_output=True, text=True, check=False)
    assert prepared.returncode == 0, prepared.stderr
    # SessionStart here uses two grandfathered owners of the legacy store.
    for story in stories:
        config_row = json.loads((story / '.auto-context/settings.json').read_text())
        qmd_shared_store._reserve_owners(qmd_shared_store.root(home),
            qmd_shared_store._collection_owners(story, config_row))
    # runtime_work intentionally uses the OS UID home, not HOME. Keep the
    # selected-store/setup path real while suppressing only test child wake
    # writes, which would otherwise reach the user's live runtime-wakes DB.
    isolation = home / 'python-isolation'
    isolation.mkdir()
    (isolation / 'sitecustomize.py').write_text(
        'import runtime_work\n'
        'runtime_work.request = lambda *args, **kwargs: {"status": "queued"}\n'
        'runtime_work.hook_wake = lambda: None\n')
    environment = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '',
                   'QMD_RECALL_LOG': '',
                   'PYTHONPATH': str(isolation) + os.pathsep + str(Path.cwd() / 'core'),
                   'PYTHONDONTWRITEBYTECODE': '1',
                   'QMD_DAEMON_PORT': '0',
                   'QMD_UPDATE_HOOK_QUEUE_DIR': str(home / 'queue')}
    calls = home / 'update-calls.txt'
    updater = home / 'fake-update.sh'
    updater.write_text('#!/bin/sh\n[ "$1" = "--resolve-only" ] && exit 0\npwd >> "' + str(calls) + '"\n')
    updater.chmod(0o700)
    manager = home / 'fake-manager.sh'
    manager.write_text('#!/bin/sh\nexit 0\n')
    manager.chmod(0o700)
    environment['QMD_CORE_UPDATE_SCRIPT'] = str(updater)
    environment['QMD_BACKEND_MANAGER'] = str(manager)
    selected = [qmd_route.project_paths(story, environ=environment, isolated=True)
                for story in stories]
    assert selected[0]['INDEX_PATH'] == selected[1]['INDEX_PATH']
    index = Path(selected[0]['INDEX_PATH'])
    with sqlite3.connect(index) as db:
        db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                   ('foreign-story', 'private.md', 'a' * 64))
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    for i, engine in enumerate(('codex', 'claude')):
        story = stories[i]
        original_home = os.environ.get('HOME')
        os.environ['HOME'] = str(home)
        try:
            assert config.find_project_config(story)['projectOwner'] == str(base)
        finally:
            if original_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = original_home
        run = subprocess.run(['bash', str(Path(__file__).resolve().parents[2] / 'hooks/run-hook'),
                              'update-queued', engine],
            cwd=story, env=environment, input=json.dumps({
                'cwd': str(story), 'hook_event_name': 'SessionStart'}),
            capture_output=True, text=True, timeout=5, check=False)
        assert run.returncode == 0, run.stderr
        key = hashlib.sha256(str(story).encode()).hexdigest()[:32]
        status = home / 'queue' / (key + '.status.json')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not status.is_file():
            time.sleep(.025)
        assert status.is_file(), (engine, run.stdout, run.stderr)
        assert json.loads(status.read_text())['status'] == 'completed'
    assert calls.read_text().splitlines() == [str(story) for story in stories], calls.read_text()
    with sqlite3.connect(index) as db:
        assert db.execute('SELECT collection,path,hash FROM documents').fetchall() == [
            ('foreign-story', 'private.md', 'a' * 64)]
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    status_key = 'f' * 32
    old_run, new_run = '1' * 32, '2' * 32
    status_path = home / 'queue' / (status_key + '.status.json')
    def phase(run_id, value):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2]
                                  / 'core/update_hook_queue.py'), 'readiness',
                                  status_key, value, run_id], env=environment,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(status_path.read_text())
    assert phase(old_run, 'indexing')['searchReady'] is False
    assert phase(old_run, 'lexical_ready')['lexicalReady'] is True
    complete = phase(old_run, 'completed')
    assert complete['vectorReady'] is True and complete['searchReady'] is False
    assert phase(new_run, 'indexing')['searchReady'] is False
    assert phase(old_run, 'completed')['runId'] == new_run
    assert phase(new_run, 'embed_failed')['searchReady'] is False
    with mock.patch.dict(os.environ, {**environment, 'QMD_DAEMON_PORT': '8484'}), mock.patch.object(
            update_hook_queue.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as health:
        update_hook_queue.readiness(status_key, 'completed', new_run)
        assert health.call_args.args[0][-1] == 'health-strict'
    assert json.loads(status_path.read_text())['searchReady'] is True
    # A pull-like new source enters the wiki queue only with an explicit
    # SessionStart grant; the independent snapshot prevents duplicate calls.
    source_dir = stories[0] / 'manuscript'
    source_dir.mkdir()
    source = source_dir / 'synthetic.md'
    source.write_text('Synthetic scene only.\n')
    settings = stories[0] / '.auto-context/settings.json'
    allowed = json.loads(settings.read_text())
    allowed['collections'].append('story-0-source')
    allowed['collectionPaths']['story-0-source'] = 'manuscript'
    allowed['collectionRoles']['story-0-source'] = 'source'
    allowed['compile'] = {'mode': 'auto-wiki', 'triggers': ['session_start_source']}
    settings.write_text(json.dumps(allowed))
    # This synthetic case begins at an already registered source owner; the
    # session exercise must not implicitly enroll a new legacy collection.
    qmd_shared_store._reserve_owners(qmd_shared_store.root(home),
        qmd_shared_store._collection_owners(stories[0], allowed))
    before_queue = stories[0] / compile_paths.rel(compile_paths.SOURCE_QUEUE)
    old_env = os.environ.copy()
    original_risky = resolve_paths.is_risky_path
    dirty = []
    dirty_patch = mock.patch.object(dirty_queue, 'enqueue_project_collections',
                                    side_effect=lambda root, changed: dirty.append(sorted(changed)))
    request_patch = mock.patch.object(runtime_work, 'request', return_value={'status': 'queued'})
    wake_patch = mock.patch.object(runtime_work, 'hook_wake', return_value=None)
    try:
        dirty_patch.start()
        request_patch.start()
        wake_patch.start()
        os.environ.update(environment)
        resolve_paths.is_risky_path = lambda _path: False
        assert sync.run(str(stories[0]), session_start=True, json_output=True) == 0
        rows = [json.loads(line) for line in before_queue.read_text().splitlines()]
        assert len(rows) == 1 and rows[0]['trigger'] == 'session_start_source'
        assert dirty == [], dirty
        assert sync.run(str(stories[0]), session_start=True) == 0
        assert len(before_queue.read_text().splitlines()) == 1
        old_stat = source.stat()
        source.write_text('Synthetic scene Xnly.\n')
        os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        assert source.stat().st_size == old_stat.st_size
        assert sync.run(str(stories[0]), session_start=True) == 0
        assert len(before_queue.read_text().splitlines()) == 2
        allowed['compile']['triggers'] = ['post_tool_source']
        settings.write_text(json.dumps(allowed))
        source.write_text('New synthetic revision.\n')
        assert sync.run(str(stories[0]), session_start=True) == 0
        assert len(before_queue.read_text().splitlines()) == 2
    finally:
        wake_patch.stop()
        request_patch.stop()
        dirty_patch.stop()
        resolve_paths.is_risky_path = original_risky
        os.environ.clear(); os.environ.update(old_env)
    # The actual update script exits zero for hook safety when its managed
    # binary is absent. The queue must retain the job and report failure.
    retry_key = hashlib.sha256(str(stories[0]).encode()).hexdigest()[:32]
    retry_job = home / 'queue' / (retry_key + '.job.json')
    retry_job.write_text(json.dumps({'schema': 'qmd-update-hook-job-v1',
                                     'cwd': str(stories[0])}))
    retry_job.chmod(0o600)
    product_env = {**environment}
    product_env.pop('QMD_CORE_UPDATE_SCRIPT')
    with mock.patch.dict(os.environ, product_env, clear=True):
        update_hook_queue.worker(retry_key)
    failed = json.loads((home / 'queue' / (retry_key + '.status.json')).read_text())
    assert failed['status'] == 'failed' and failed['searchReady'] is False
    assert retry_job.is_file()

# Clean install: no project settings and no selected plugin store. The worker
# reports setup_required, KEEPS the job for a later SessionStart and returns
# the explicit result; the update handler ACKs only that wake. Every other
# store error, unreadable settings, and a configured project still fail closed.
import runtime_handlers
import setup_guard
with tempfile.TemporaryDirectory(prefix='qmd-clean-start-', dir=Path.cwd().parent) as temp:
    home = Path(temp).resolve()
    project = home / 'fresh-project'
    project.mkdir()
    queue = home / 'queue'
    clean_env = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '',
                 'QMD_RECALL_LOG': '', 'QMD_UPDATE_HOOK_QUEUE_DIR': str(queue)}
    key = hashlib.sha256(str(project).encode()).hexdigest()[:32]
    job = queue / (key + '.job.json')
    status_path = queue / (key + '.status.json')
    settings_dir = project / '.auto-context'
    def write_job():
        queue.mkdir(mode=0o700, exist_ok=True)
        job.write_text(json.dumps({'schema': 'qmd-update-hook-job-v1', 'cwd': str(project)}))
        job.chmod(0o600)
    def expect_raise(message):
        try:
            update_hook_queue.worker(key)
        except ValueError as exc:
            assert str(exc) == message, exc
        else:
            raise AssertionError(message + ' did not fail closed')
        assert job.is_file() and not status_path.exists()
    assert not qmd_shared_store.root(home).exists()
    with mock.patch.dict(os.environ, clean_env, clear=True), mock.patch.object(
            runtime_work, 'request', side_effect=AssertionError('no retry wake')):
        assert config.find_project_config(str(project))['configFormat'] == 'none'
        assert setup_guard.status(str(project))['status'] == 'ready'
        try:
            qmd_route.project_paths(str(project))
        except ValueError as exc:
            assert str(exc) == 'plugin_qmd_store_not_ready', exc
        else:
            raise AssertionError('clean state unexpectedly has a selected store')
        write_job()
        assert update_hook_queue.worker(key) == update_hook_queue.SETUP_REQUIRED
        clean = json.loads(status_path.read_text())
        assert clean['status'] == 'setup_required', clean
        assert clean['reason'] == 'plugin_qmd_store_not_selected', clean
        assert job.is_file(), 'clean install must keep the job for a later SessionStart'
        assert not qmd_shared_store.root(home).exists()
        # The wake handler ACKs this setup-required wake although the job stays.
        assert runtime_handlers.update({'target': key, 'revision': 1}) is True
        assert job.is_file()
        status_path.unlink()
        # An unsafe/corrupt store is not a first install: it must still raise.
        with mock.patch.object(qmd_route, 'project_paths',
                               side_effect=ValueError('unsafe_plugin_qmd_store')):
            expect_raise('unsafe_plugin_qmd_store')
        # Unreadable settings read as configFormat 'none' must not pass, even
        # if the top-level guard were bypassed.
        settings_dir.mkdir()
        (settings_dir / 'settings.json').write_text('{not json')
        assert config.find_project_config(str(project))['configFormat'] == 'none'
        assert setup_guard.status(str(project))['status'] == 'setup_required'
        assert update_hook_queue.worker(key) is None
        assert runtime_handlers.update({'target': key, 'revision': 2}) is False
        assert job.is_file()
        status_path.unlink()
        with mock.patch.object(setup_guard, 'status', return_value={'status': 'ready'}):
            expect_raise('plugin_qmd_store_not_ready')
        # A configured project without its store is not a first install either.
        (settings_dir / 'settings.json').write_text(json.dumps({
            'indexing': True, 'collections': ['fresh-wiki'],
            'collectionPaths': {'fresh-wiki': 'wiki'}}))
        with mock.patch.object(setup_guard, 'status', return_value={'status': 'ready'}):
            expect_raise('plugin_qmd_store_not_ready')
            # After setup selects a store, the retained job is processed by
            # the next wake. The store route and updater are synthetic stubs.
            updater = home / 'fake-update.sh'
            updater.write_text('#!/bin/sh\nexit 0\n')
            updater.chmod(0o700)
            with mock.patch.dict(os.environ, {'QMD_CORE_UPDATE_SCRIPT': str(updater)}), \
                    mock.patch.object(qmd_route, 'project_paths', return_value={'selected': True}):
                assert runtime_handlers.update({'target': key, 'revision': 3}) is True
        assert json.loads(status_path.read_text())['status'] == 'completed'
        assert not job.exists()
print('shared-sessionstart-ok')
