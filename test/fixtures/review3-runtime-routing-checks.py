"""Review-3 regressions using only temporary QMD packages and SQLite indexes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'core')
import qmd_route
import qmd_runtime
import runtime_update
import topical_hook_queue
import wiki_topical as topical
import wiki_topical_publish as publisher
from context_learning.corpus import snapshot
from context_learning.seam import observe
from context_learning.contracts import digest


def make_index(path, document_hash):
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);'
            'CREATE TABLE content(hash TEXT);'
            'CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT);'
            'CREATE TABLE store_collections(name TEXT);'
            'CREATE TABLE vectors_vec(embedding "float[768] distance_metric=cosine");')
        db.execute('INSERT INTO documents VALUES (?,?,?,1)', ('docs', 'note.md', document_hash))
        db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',
                   (document_hash, 0, 'synthetic-model', 'synthetic-fingerprint'))


def project_pointer(base):
    root = base / 'project'; root.mkdir(mode=0o700)
    auto = root / '.auto-context'; auto.mkdir(mode=0o700)
    (root / topical.MARKER).write_text('synthetic only\n')
    docs = root / 'docs'; docs.mkdir()
    source = docs / 'note.md'; source.write_text('Selected synthetic source.\n')
    config = {'collections': ['docs'], 'collectionPaths': {'docs': 'docs'},
              'collectionRoles': {'docs': 'raw'}, 'recallStrategy': 'flat',
              'topN': 1, 'minScore': 0, 'contextLearning': {
                  'capture': True, 'stateRoot': str(base / 'private')}}
    (auto / 'settings.json').write_text(json.dumps(config))
    selected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = root / 'original.sqlite'; make_index(original, 'a' * 64)
    generation = auto / 'qmd-index-generations/g-synthetic'; generation.mkdir(parents=True)
    selected = generation / 'index.sqlite'; make_index(selected, selected_hash)
    cfg_dir = generation / 'config'; cfg_dir.mkdir()
    cfg = cfg_dir / 'index.yml'; cfg.write_text('collections: {wiki: synthetic}\n')
    (generation / 'cache').mkdir()
    inspection = runtime_update.inspect_index(selected)
    prepared = generation / 'prepared.json'
    prepared.write_text(json.dumps({'schema': 'qmd-shadow-index-v1',
        'index': str(selected), 'inspection': inspection,
        'configSha256': hashlib.sha256(cfg.read_bytes()).hexdigest()}))
    prepared.chmod(0o600)
    pointer = auto / 'qmd-index-active.json'
    pointer.write_text(json.dumps({'schema': 'qmd-index-pointer-v1',
        'generation': str(generation), 'index': str(selected),
        'preparedSha256': hashlib.sha256(prepared.read_bytes()).hexdigest()}))
    pointer.chmod(0o600)
    return root, config, original, selected, selected_hash


results = {}
with tempfile.TemporaryDirectory(prefix='qmd-review3-') as temporary:
    base = Path(temporary).resolve()
    home = base / 'home'; home.mkdir(mode=0o700)
    host_node = Path(shutil.which('node') or '')
    assert host_node.is_file(), 'installed Node is required for the synthetic daemon probe'
    node = base / 'synthetic-node24'
    node.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "v24.0.0\\n"; '
        'else exec ' + shlex.quote(str(host_node.resolve())) + ' "$@"; fi\n')
    node.chmod(0o700)
    package = base / 'package'; (package / 'bin').mkdir(parents=True)
    (package / 'dist/cli').mkdir(parents=True)
    (package / 'node_modules/better-sqlite3').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'version': '2.5.3'}))
    (package / 'node_modules/better-sqlite3/index.js').write_text('module.exports = {};\n')
    qmd = package / 'bin/qmd'
    qmd.write_text('''#!/usr/bin/env node
if (process.argv[2] === '--version') console.log('qmd 2.5.3');
else if (process.argv[2] === '--help') console.log('qmd vsearch\\nqmd search\\nqmd get\\nqmd collection add\\nqmd update\\nqmd embed');
''')
    qmd.chmod(0o700)
    marker = base / 'daemon-call.json'
    entry = package / 'dist/cli/qmd.js'
    entry.write_text('require("node:fs").writeFileSync(process.env.QMD_SYNTHETIC_MARKER, JSON.stringify({entry: __filename, args: process.argv.slice(2)}));\n')
    runtime_root = qmd_runtime.managed_root(home)
    with patch.dict(os.environ, {'HOME': str(home)}, clear=False):
        staged = qmd_runtime.prepare_existing(runtime_root, qmd, node)
        qmd_runtime.activate(runtime_root, staged['generation'])
        info = qmd_route.binary_info()
        assert info['QMD_ENTRY'] == str(entry) and info['QMD_PACKAGE_BIN'] == str(qmd)
        assert info['QMD_NODE_BIN'] == str(node.resolve())
        env = {**os.environ, 'QMD_SYNTHETIC_MARKER': str(marker), 'QMD_DAEMON_PORT': '18483'}
        env.pop('QMD_BIN', None); env.pop('QMD_NODE_BIN', None)
        daemon = subprocess.run(['bash', 'backend/daemon.sh'], env=env,
                                capture_output=True, text=True, timeout=15)
        assert daemon.returncode == 0, daemon.stderr
        assert json.loads(marker.read_text()) == {'entry': str(entry),
                                                   'args': ['mcp', '--http', '--port', '18483']}
    results['F1_managedDaemonEntryAndPackageNativeProbe'] = True

    root, config, original, selected, selected_hash = project_pointer(base)
    original_bytes = original.read_bytes()
    (base / 'private').mkdir(mode=0o700)
    fake_qmd = base / 'fake-qmd'
    calls = base / 'calls.jsonl'
    fake_qmd.write_text('''#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ['--version']: print('qmd 2.5.3')
elif sys.argv[1:] == ['--help']:
 print('qmd vsearch\\nqmd search\\nqmd get\\nqmd collection add\\nqmd update\\nqmd embed')
else:
 with open(os.environ['QMD_SYNTHETIC_CALLS'], 'a') as out:
  out.write(json.dumps({key: os.environ.get(key) for key in ('INDEX_PATH','QMD_CONFIG_DIR','XDG_CACHE_HOME')})+'\\n')
''')
    fake_qmd.chmod(0o700)
    isolated_home = base / 'isolated-home'; isolated_home.mkdir(mode=0o700)
    env = {'HOME': str(isolated_home), 'QMD_BIN': str(fake_qmd),
           'QMD_SETUP_GUARD_FIXTURE': '1',
           'QMD_SYNTHETIC_CALLS': str(calls), 'INDEX_PATH': str(original),
           'QMD_CONFIG_DIR': str(root / 'old-config'),
           'XDG_CACHE_HOME': str(root / 'old-cache')}
    with patch.dict(os.environ, env, clear=False):
        route = qmd_route.project_paths(root)
        assert route['INDEX_PATH'] == str(selected) and route['selected']
        try:
            publisher._qmd_runtime(root)
        except topical.TopicalError as exc:
            assert exc.code == 'project_index_env_conflict'
        else:
            raise AssertionError('publisher accepted stale inherited DB')
    for key in qmd_route.PATH_KEYS:
        env.pop(key)
    with patch.dict(os.environ, env, clear=True):
        route = qmd_route.project_paths(root)
        publication = publisher._qmd_runtime(root)
        assert all(str(publication[key]) == route[key] for key in qmd_route.PATH_KEYS)
        wiki = root / '.auto-context/wiki'; wiki.mkdir()
        with patch.object(publisher, '_project', return_value=(root, wiki, 'wiki')):
            assert publisher.sync(root, allow_empty=True)['status'] == 'ready'
        assert json.loads(calls.read_text().splitlines()[-1]) == {
            key: route[key] for key in qmd_route.PATH_KEYS}
    assert original.read_bytes() == original_bytes
    results['F2_publisherSelectedTupleAndConflictGuard'] = True

    with patch.dict(os.environ, {'HOME': str(home), 'INDEX_PATH': str(original),
                                 'QMD_CONFIG_DIR': str(root / 'old-config')}, clear=False):
        (root / 'old-config').mkdir()
        (root / 'old-config/index.yml').write_text('models: old\n')
        global_snap = snapshot(root, config, qmd_paths={
            'INDEX_PATH': str(original), 'QMD_CONFIG_DIR': str(root / 'old-config'),
            'XDG_CACHE_HOME': str(root / 'old-cache'), 'selected': False,
            'generation': None})
        selected_route = qmd_route.project_paths(root)
        selected_snap = snapshot(root, config, qmd_paths=selected_route)
        default_snap = snapshot(root, config)
        assert selected_snap == default_snap and selected_snap != global_snap
        assert selected_snap['material']['activeDocuments'][0][2] == selected_hash
        assert selected_snap['material']['runtimeGeneration'] == selected_route['generation']
        same_content_new_generation = snapshot(root, config, qmd_paths={
            **selected_route, 'generation': str(root / '.auto-context/qmd-index-generations/g-next')})
        assert same_content_new_generation['fingerprint'] != selected_snap['fingerprint']
        hit = {'file': 'docs/note.md', 'score': 1.0}
        status = observe({'prompt': 'Selected synthetic source'}, config, root, [],
            phases=[{'name': 'primary', 'results': [hit], 'wiki_scoped': False,
                     'returned_count': 1}], verdict=lambda *_: 'eligible',
            qmd_paths=selected_route)
        assert status == 'stored', status
        state = base / 'private' / digest(str(root))
        with sqlite3.connect(state / 'learning.sqlite3') as db:
            stored = json.loads(db.execute('SELECT body FROM samples').fetchone()[0])
        assert stored['sampling']['index_revision'] == selected_snap['fingerprint']
    results['F3_searchCaptureFingerprintSameSelectedGeneration'] = True

    queue_root = base / 'queue-project'; queue_root.mkdir(mode=0o700)
    (queue_root / topical.MARKER).write_text('synthetic only\n')
    queue = queue_root / topical_hook_queue.QUEUE; queue.mkdir(mode=0o700)
    first = queue / 'first.json'
    first.write_text(json.dumps({'schema': 'topical-hook-job-v1',
                                 'action': 'reconcile', 'turnKey': 'first'}))
    state = {'calls': 0, 'lockRejected': False}
    def losing_child(_root):
        probe = subprocess.run([sys.executable, '-c',
            'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDONLY); '
            'fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            str(queue / '.worker.lock')], capture_output=True, text=True)
        state['lockRejected'] = probe.returncode != 0
        return SimpleNamespace()
    def run_job(_root, _job):
        state['calls'] += 1
        if state['calls'] == 1:
            topical_hook_queue.enqueue(queue_root, 'reconcile')
        return None
    with patch.object(topical_hook_queue, '_spawn_worker', side_effect=losing_child), \
         patch.object(topical_hook_queue, '_run', side_effect=run_job):
        topical_hook_queue.worker(queue_root)
    assert state['lockRejected'] and state['calls'] == 2
    assert not list(queue.glob('*.json'))
    assert json.loads((queue_root / 'topical-hook-status.json').read_text())['status'] == 'completed'
    results['F4_losingChildJobDrained'] = True

print(json.dumps(results, sort_keys=True))
