"""No real QMD/model/provider. Exercise the shipped command hook and update core."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import runpy
from unittest.mock import patch


repository = Path.cwd().resolve()
with tempfile.TemporaryDirectory(prefix='qmd-nested-index-', dir=repository) as temp:
    home = Path(temp).resolve()
    os.environ['HOME'] = str(home)
    sys.path.insert(0, str(repository / 'core'))
    from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME
    from context_learning.synthetic_scope import project_collection_paths

    novel = home / 'work/novel'
    story = novel / PLANNED_BUNDLE_NAME
    nested = story / '01_Settings/qmd-probes'
    nested.mkdir(parents=True)
    source = nested / 'ep077-settings-probe.md'
    source.write_text('Synthetic settings probe only.\n')
    (story / '02_Characters').mkdir()
    (story / '03_Plot').mkdir()
    (story / '04_Manuscript').mkdir()
    (story / '.auto-context/wiki').mkdir(parents=True)
    (novel / '.auto-context/wiki').mkdir(parents=True)
    settings = novel / '.auto-context/settings.json'
    paths = project_collection_paths(novel)
    settings.write_text(json.dumps({
        'indexing': True, 'collections': list(paths), 'collectionPaths': paths,
        'events': {'sessionStart': True, 'postToolUse': True},
        'compile': {'enabled': False},
    }))
    # The production CLI still requires an exact settings owner. The worker
    # must supply that owner rather than relaxing this boundary.
    import qmd_shared_store
    registered = []
    def route_register(path):
        with patch.object(qmd_shared_store, 'register_config',
                          side_effect=lambda root, config, **_: registered.append(root)), \
             patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': ''}), \
             patch.object(sys, 'argv', ['qmd_route.py', 'register-config', str(path)]):
            try:
                runpy.run_path(str(repository / 'core/qmd_route.py'), run_name='__main__')
            except SystemExit as exc:
                return exc.code
        return 0
    assert route_register(nested) == 1 and not registered
    assert route_register(novel) == 0 and registered == [str(novel)]
    # Exercise the production registration/enrollment route against a private
    # prepared store. A nested cwd must resolve to novel; the nested path
    # itself may never become a collection owner.
    prepared = subprocess.run([sys.executable, str(repository /
        'test/fixtures/prepare-shared-qmd-store.py'), str(home)],
        cwd=repository, capture_output=True, text=True, check=False)
    assert prepared.returncode == 0, prepared.stderr
    # This legacy fixture represents a grandfathered, already registered
    # owner. Fresh owners require the reviewed enrollment transition.
    qmd_shared_store._reserve_owners(qmd_shared_store.root(home),
        qmd_shared_store._collection_owners(novel,
            json.loads(settings.read_text())))
    def production_route(*args):
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': ''}), \
             patch.object(sys, 'argv', ['qmd_route.py', *map(str, args)]):
            try:
                runpy.run_path(str(repository / 'core/qmd_route.py'), run_name='__main__')
            except SystemExit as exc:
                return exc.code
        return 0
    assert production_route('register-config', nested) == 1
    assert production_route('register-config', novel) == 0
    ownership = qmd_shared_store._registry(qmd_shared_store.root(home))
    assert set(ownership) == set(paths)
    assert all(row['scope'] == str(novel) for row in ownership.values())
    assert production_route('verify-shared-enrollment') == 0
    bin_dir = home / 'bin'
    bin_dir.mkdir()
    manager = bin_dir / 'manager.sh'
    manager.write_text('#!/bin/sh\nexit 0\n')
    manager.chmod(0o700)
    queue = home / 'dirty-queue'
    env = {**os.environ, 'QMD_SETUP_GUARD_FIXTURE': '1',
           'QMD_BACKEND_MANAGER': str(manager), 'QMD_DIRTY_QUEUE': str(queue),
           'QMD_CACHE_DIR': str(home / 'cache'), 'QMD_RECALL_LOG': '',
           'QMD_AUTO_COMPILE_DISABLED': '1', 'QMD_SKIP_BACKGROUND_EMBED': '1',
           'QMD_LOCK_BASE': str(home / 'locks')}
    payload = {'hook_event_name': 'PostToolUse', 'cwd': str(nested),
               'tool_name': 'apply_patch', 'tool_input': {'command':
               '*** Begin Patch\n*** Add File: ep077-settings-probe.md\n+Synthetic settings probe only.\n*** End Patch'}}
    hook = subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'index', 'codex'],
                          input=json.dumps(payload), text=True, capture_output=True,
                          cwd=nested, env=env, timeout=5, check=False)
    assert hook.returncode == 0, hook.stderr
    assert queue.is_file(), (hook.stdout, hook.stderr)
    expected = ('fantasy-20261008-settings\t' + str(story / '01_Settings'))
    assert queue.read_text().strip() == expected, queue.read_text()
    private = novel / 'other-private-story'
    private.mkdir()
    blocked = {**payload, 'tool_input': {'command':
               '*** Begin Patch\n*** Add File: ../../other-private-story/private.md\n+private\n*** End Patch'}}
    hook = subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'index', 'codex'],
                          input=json.dumps(blocked), text=True, capture_output=True,
                          cwd=nested, env=env, timeout=5, check=False)
    assert hook.returncode == 0 and queue.read_text().strip() == expected

    qmd_log = home / 'qmd.log'
    fake_qmd = bin_dir / 'qmd'
    fake_qmd.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(qmd_log) + '"\n'
                        'case "$1 $2" in\n'
                        '  "collection list") exit 0 ;;\n'
                        '  "collection show") exit 1 ;;\n'
                        '  *) exit 0 ;;\n'
                        'esac\n')
    fake_qmd.chmod(0o700)
    readiness = home / 'readiness.log'
    real_python = sys.executable
    py_wrapper = bin_dir / 'python3'
    py_wrapper.write_text('#!/bin/sh\ncase "$1" in\n'
                          '  */update_hook_queue.py) [ "$2" = readiness ] && '
                          '{ echo "$4" >> "' + str(readiness) + '"; exit 0; } ;;\n'
                          'esac\nexec "' + real_python + '" "$@"\n')
    py_wrapper.chmod(0o700)
    worker_env = {**env, 'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                  'QMD_BIN': str(fake_qmd), 'QMD_UPDATE_HOOK_KEY': 'a' * 32,
                  'QMD_UPDATE_HOOK_RUN_ID': 'b' * 32,
                  'QMD_HOOK_LOG': str(home / 'hook.log')}
    worker = subprocess.run(['bash', str(repository / 'core/update.sh'), '--worker', str(nested)],
                            text=True, capture_output=True, cwd=nested, env=worker_env,
                            timeout=8, check=False)
    assert worker.returncode == 0, (worker.stdout, worker.stderr)
    commands = qmd_log.read_text()
    assert 'collection add ' + str(story / '01_Settings') in commands, commands
    assert 'update' in commands, commands
    assert readiness.is_file(), (commands, (home / 'hook.log').read_text(),
                                 worker.stdout, worker.stderr)
    assert 'lexical_ready' in readiness.read_text(), readiness.read_text()
    print(json.dumps({'nestedEditQueued': True, 'canonicalCollectionPath': True,
                      'productionEnrollment': True, 'lexicalReady': True}))
