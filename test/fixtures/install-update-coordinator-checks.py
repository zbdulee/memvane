"""CLI-phase setup E2E with only synthetic projects, fake QMD, and temp DBs."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import shlex
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, 'core')
import install_update as setup
import setup_entry
import config as project_config
import qmd_installer
import qmd_runtime
import qmd_route
import runtime_update
import wiki_topical
import wiki_topical_backend as topical_backend
import wiki_topical_publish as topical_publisher


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cli(action, project, request=None):
    args = [action, '--project', str(project)]
    if request: args += ['--request', str(request)]
    output = io.StringIO()
    previous = {key: os.environ.get(key) for key in ('HOME', 'PATH', 'QMD_BACKEND_MANAGER')}
    os.environ['HOME'] = str(project.parent / ('home-' + project.name))
    os.environ['PATH'] = str(project.parent / 'bin') + os.pathsep + (previous['PATH'] or '')
    os.environ['QMD_BACKEND_MANAGER'] = str(project.parent / 'fake-manager.sh')
    try:
        with redirect_stdout(output): code = setup.main(args)
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
    result = json.loads(output.getvalue())
    return code, result


def entry_cli(action, project, *options):
    args = [action, '--project', str(project), *map(str, options)]
    output = io.StringIO()
    previous = {key: os.environ.get(key) for key in ('HOME', 'PATH', 'QMD_BACKEND_MANAGER')}
    os.environ['HOME'] = str(project.parent / ('home-' + project.name))
    os.environ['PATH'] = str(project.parent / 'bin') + os.pathsep + (previous['PATH'] or '')
    os.environ['QMD_BACKEND_MANAGER'] = str(project.parent / 'fake-manager.sh')
    try:
        with redirect_stdout(output): code = setup_entry.main(args)
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
    return code, json.loads(output.getvalue())


def make_index(path, wiki=None):
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);'
            'CREATE TABLE content(hash TEXT);'
            'CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT);'
            'CREATE TABLE store_collections(name TEXT);'
            'CREATE TABLE vectors_vec(embedding "float[768] distance_metric=cosine");')
        pages = sorted(Path(wiki).rglob('*.md')) if wiki else []
        if not pages: pages = [None]
        for page in pages:
            relative = page.relative_to(wiki).as_posix() if page else 'card.md'
            content_sha = sha(page) if page else 'synthetic-hash'
            db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                       ('synthetic', relative, content_sha))
            db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',
                       (content_sha, 0, 'synthetic-model', 'fingerprint'))


with tempfile.TemporaryDirectory(prefix='qmd-setup-cli-e2e-') as temporary:
    base = Path(temporary).resolve()
    node_host = shutil.which('node')
    assert node_host
    node = base / 'synthetic-node'
    node.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "v24.0.0\\n"; '
                    'else exec ' + json.dumps(str(Path(node_host).resolve())) + ' "$@"; fi\n')
    node.chmod(0o700)
    package = base / 'existing-qmd';(package / 'bin').mkdir(parents=True)
    (package / 'package.json').write_text('{"version":"2.5.3"}\n')
    qmd = package / 'bin/qmd'
    qmd.write_text('if (process.argv.includes("--version")) console.log("qmd 2.5.3");'
        ' else if (process.argv.includes("--help")) console.log(' +
        json.dumps(' '.join(qmd_runtime.CAPABILITIES)) + ');\n')
    qmd.chmod(0o700)
    global_bin = base / 'bin'; global_bin.mkdir()
    global_qmd = global_bin / 'qmd'
    global_qmd.write_text('#!/bin/sh\nexec ' + shlex.quote(str(node)) + ' ' +
                          shlex.quote(str(qmd)) + ' "$@"\n')
    global_qmd.chmod(0o700)
    manager = base / 'fake-manager.sh'
    route_script = shlex.quote(str(Path.cwd() / 'core/qmd_route.py'))
    manager.write_text(r'''#!/bin/bash
set -eu
state="$HOME/.fake-daemon"
case "$1" in
  identity) [ -f "$state" ] || exit 1; cat "$state"; [ ! -f "$HOME/.unhealthy-daemon" ] || exit 3 ;;
  reload)
    if [ -f "$HOME/.fail-next-reload" ]; then
      rm "$HOME/.fail-next-reload"
      exit 1
    fi
    rm -f "$HOME/.unhealthy-daemon"
    spec="$(python3 __ROUTE__ daemon-spec)"
    IFS=$'\037' read -r entry node package <<< "$spec"
    before=0
    [ ! -f "$state" ] || before="$(cut -f1 "$state")"
    printf '%s\t%s %s mcp --http --port 19483\n' "$((before+1))" "$node" "$entry" >"$state" ;;
  *) exit 2 ;;
esac
'''.replace('__ROUTE__', route_script))
    manager.chmod(0o700)
    original_stage = runtime_update.stage_shadow_index
    fail_embed = [False]
    def runner(command, *, env, cwd, **_):
        assert Path(env['INDEX_PATH']).is_relative_to(base)
        if command[1] == 'update':
            target_wiki = None if Path(cwd).name == 'wrong-corpus' else Path(cwd) / '.auto-context/wiki'
            make_index(env['INDEX_PATH'], target_wiki)
        if command[1] == 'embed' and fail_embed[0]:
            return SimpleNamespace(returncode=1, stdout='', stderr='synthetic interrupted embed')
        return SimpleNamespace(returncode=0,
                               stdout='[]' if command[1] == 'vsearch' else '', stderr='')
    def stage(*args, **kwargs):
        return original_stage(*args, **kwargs, runner=runner)
    runtime_update.stage_shadow_index = stage
    setup.supported_platform = lambda: True

    def project(name, *, legacy=True, v1=False):
        root = base / name;root.mkdir(mode=0o700)
        (root / wiki_topical.MARKER).write_text('synthetic only\n')
        auto = root / '.auto-context';auto.mkdir(mode=0o700)
        wiki = auto / 'wiki';wiki.mkdir(mode=0o700)
        (wiki / 'index.md').write_text('Synthetic wiki index.\n')
        if v1:
            (wiki / 'v1.md').write_text('---\nschemaVersion: 1\nstatus: verified\n---\nold card\n')
        settings = {'indexing': True, 'collections': ['synthetic'],
                    'collectionPaths': {'synthetic': '.auto-context/wiki'},
                    'collectionRoles': {'synthetic': 'wiki'}, 'recallStrategy': 'wikiOnly'}
        settings_path = (root / '.agents/qmd-recall.json' if legacy == 'agents' else
                         root / '.auto-context.json' if legacy else
                         root / '.auto-context/settings.json')
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings))
        config = root / 'qmd-config/index.yml';config.parent.mkdir()
        config.write_text('collections: {synthetic: {path: ' + str(wiki) + '}}\n'
                          'models:\n  embed: synthetic-model\n')
        cache = root / 'qmd-cache/qmd/models';cache.mkdir(parents=True)
        old = root / 'old.sqlite';make_index(old, wiki)
        private = auto / 'context-learning';private.mkdir(mode=0o700)
        preserved = []
        for name in ('checkpoint.bin', 'gold.jsonl', 'history.jsonl'):
            path = private / name;path.write_bytes(('synthetic ' + name).encode());preserved.append(path)
        return root, config, wiki, cache.parent.parent, old, preserved

    def request(root, config, wiki, cache, old, *, qmd_mode='reuse', index_mode='shadow',
                laya_mode='defer', config_mode='copy_legacy', wiki_mode='preserve'):
        qmd_root = qmd_runtime.managed_root(base / ('home-' + root.name))
        qmd_item = ({'mode': 'reuse', 'qmd': str(qmd), 'node': str(node)} if qmd_mode == 'reuse'
                    else {'mode': 'active'} if qmd_mode == 'active' else
                    {'mode': 'install', 'node': str(node), 'npm': str(base / 'fake-npm'),
                     'allowExecution': True, 'allowLifecycleScripts': True})
        index_item = ({'mode': 'none'} if index_mode == 'none' else
                      {'mode': 'shadow', 'config': str(config), 'wikiDir': str(wiki),
                       'modelCache': str(cache), 'sourceIndex': str(old),
                       'expectedModel': 'synthetic-model', 'expectedDimension': 768,
                       'allowExecution': True})
        data = {'schema': setup.REQUEST_SCHEMA, 'project': str(root),
            'runtimeRoots': {'qmd': str(qmd_root), 'laya': str(base / ('laya-' + root.name))},
            'qmd': qmd_item, 'laya': {'mode': laya_mode}, 'index': index_item,
            'config': config_mode, 'wiki': {'mode': wiki_mode}}
        path = base / (root.name + '-request.json');path.write_text(json.dumps(data));path.chmod(0o600)
        return path, data

    competing, _, _, _, _, _ = project('migration-lock-race')
    with setup._locked(competing):
        blocked = project_config.migrate_legacy_config(competing)
    assert blocked['reason'] == 'managed_setup_in_progress', blocked
    assert (competing / '.auto-context.json').exists()

    root, config, wiki, cache, old, preserved = project('legacy')
    prior_which = shutil.which
    prior_qmd_root = qmd_runtime.managed_root
    prior_laya_root = setup.laya_setup.default_managed_root
    shutil.which = lambda _: None
    qmd_runtime.managed_root = lambda: base / 'read-only-qmd-root'
    setup.laya_setup.default_managed_root = lambda: base / 'read-only-laya-root'
    code, inventory = cli('inspect', root)
    assert code == 0 and inventory['status'] == 'ready_to_plan', inventory
    assert inventory['changesApplied'] == 0
    assert inventory['defaultRuntimeRoots']['qmd'] == str(base / 'read-only-qmd-root')
    assert not (root / '.auto-context/install-update-journal.json').exists()
    shutil.which, qmd_runtime.managed_root = prior_which, prior_qmd_root
    setup.laya_setup.default_managed_root = prior_laya_root
    daemon_home = base / ('home-' + root.name)
    daemon_home.mkdir(mode=0o700)
    daemon_state = daemon_home / '.fake-daemon'
    daemon_state.write_text('900\t' + str(node) + ' ' + str(global_qmd) +
                            ' mcp --http --port 19483\n')
    original = {p: p.read_bytes() for p in [old, root / '.auto-context.json', *preserved]}
    req, data = request(root, config, wiki, cache, old)
    code, first = cli('prepare', root, req)
    assert code == 0 and first['status'] == 'prepared', first
    migration = project_config.migrate_legacy_config(root)
    assert migration == {'migrated': False, 'reason': 'managed_setup_in_progress',
        'from': str(root / '.auto-context.json'),
        'to': str(root / '.auto-context/settings.json')}, migration
    assert (root / '.auto-context.json').read_bytes() == original[root / '.auto-context.json']
    code, repeat = cli('prepare', root, req)
    assert code == 0 and repeat['staged'] == first['staged']
    assert not (Path(data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert not (root / '.auto-context/qmd-index-active.json').exists()
    code, active = cli('activate', root)
    assert code == 0 and active['status'] == 'activated', active
    routed = qmd_route.binary_info({'HOME': str(base / ('home-' + root.name)),
                                    'PATH': os.environ.get('PATH', '')})
    assert routed['managed'] and routed['QMD_BIN'] == first['staged']['qmd']['wrapper']
    # Both host manifests register the same actions against distinct host labels.
    repository = Path.cwd()
    host_manifests = {'codex': repository / 'hooks/hooks-codex.json',
                      'claude': repository / 'hooks/hooks.json'}
    for host, manifest in host_manifests.items():
        hooks = json.loads(manifest.read_text())['hooks']
        commands = [entry['command'] for event in hooks.values()
                    for row in event for entry in row['hooks']]
        assert all(command.endswith(' ' + host) for command in commands)
        assert any('update-queued ' + host in command for command in commands)
        assert any('recall ' + host in command for command in commands)
    assert (repository / 'skills/setup/SKILL.md').is_file()
    assert json.loads((repository / '.codex-plugin/plugin.json').read_text())['hooks'] == './hooks/hooks-codex.json'
    assert (repository / '.claude-plugin/plugin.json').is_file()
    host_probe = base / 'host-probe.py'
    host_probe.write_text('import json,os,sys\n'
        'sys.path.insert(0, ' + repr(str(repository / 'core')) + ')\n'
        'import config,qmd_route\n'
        'root=json.load(sys.stdin)["cwd"]\n'
        'print(json.dumps({"engine":os.environ["QMD_ENGINE"],'
        '"route":qmd_route.project_paths(root),'
        '"bin":qmd_route.binary_info()["QMD_BIN"],'
        '"indexing":config.load_project_config(root)["indexing"]}))\n')
    hook_env = {**os.environ, 'HOME': str(daemon_home),
        'PATH': str(global_bin) + os.pathsep + os.environ.get('PATH', ''),
        'CLAUDE_PLUGIN_ROOT': str(repository), 'PLUGIN_ROOT': str(repository),
        'QMD_CORE_RECALL_SCRIPT': str(host_probe), 'QMD_QUERY_FIXTURE': 'synthetic',
        'QMD_BACKEND_MANAGER': str(manager), 'QMD_RECALL_LOG': '',
        'PYTHONDONTWRITEBYTECODE': '1'}
    host_reports = {}
    for host in ('codex', 'claude'):
        completed = subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'recall', host],
            cwd=root, env=hook_env, input=json.dumps({'cwd': str(root), 'prompt': 'Synthetic wiki check'}),
            text=True, capture_output=True, timeout=15, check=True)
        host_reports[host] = json.loads(completed.stdout)
        assert host_reports[host]['engine'] == host
        assert host_reports[host]['indexing'] is True
        assert host_reports[host]['route']['INDEX_PATH'] == first['staged']['index']['index']
        assert host_reports[host]['bin'] == first['staged']['qmd']['wrapper']
    assert host_reports['codex']['route'] == host_reports['claude']['route']
    optout, _, _, _, _, _ = project('host-optout', legacy=False)
    (optout / '.auto-context/settings.json').write_text(json.dumps(
        {'indexing': False, 'collections': [], 'events': []}))
    optout_env = dict(hook_env, HOME=str(base / 'home-host-optout'))
    for host in ('codex', 'claude'):
        completed = subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'recall', host],
            cwd=optout, env=optout_env,
            input=json.dumps({'cwd': str(optout), 'prompt': 'Synthetic optout check'}),
            text=True, capture_output=True, timeout=15, check=True)
        assert json.loads(completed.stdout)['indexing'] is False
    # Two SessionStart entries coalesce through one project queue and use the selected DB.
    update_stub = base / 'host-update-stub.sh'
    update_calls = base / 'host-update-calls.txt'
    update_stub.write_text('#!/bin/sh\npython3 ' + shlex.quote(str(repository / 'core/qmd_route.py')) +
        ' resolve-env "$PWD" >> ' + shlex.quote(str(update_calls)) + '\nsleep 0.3\n')
    update_stub.chmod(0o700)
    hook_env.update(QMD_CORE_UPDATE_SCRIPT=str(update_stub),
                    QMD_UPDATE_HOOK_QUEUE_DIR=str(base / 'host-update-queue'),
                    QMD_SUPPRESS_NOTICE='1')
    for host in ('codex', 'claude'):
        subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'update-queued', host],
            cwd=root, env=hook_env, input=json.dumps({'cwd': str(root)}),
            text=True, capture_output=True, timeout=15, check=True)
    limit = time.monotonic() + 10
    statuses = list((base / 'host-update-queue').glob('*.status.json'))
    while not statuses and time.monotonic() < limit:
        time.sleep(.05)
        statuses = list((base / 'host-update-queue').glob('*.status.json'))
    assert len(statuses) == 1 and json.loads(statuses[0].read_text())['status'] == 'completed'
    assert update_calls.read_text().count(first['staged']['index']['index']) == 1
    for host in ('codex', 'claude'):
        completed = subprocess.run(['bash', str(repository / 'hooks/run-hook'), 'recall', host],
            cwd=root, env=hook_env,
            input=json.dumps({'cwd': str(root), 'prompt': 'After shared synthetic update'}),
            text=True, capture_output=True, timeout=15, check=True)
        again = json.loads(completed.stdout)
        assert again['route'] == host_reports[host]['route']
        assert again['bin'] == host_reports[host]['bin']
    assert all(p.read_bytes() == body for p, body in original.items())
    assert cli('activate', root)[1]['status'] == 'activated_unchanged'
    assert daemon_state.read_text().startswith('901\t')
    assert str(qmd) in daemon_state.read_text()
    selected_pointer = Path(data['runtimeRoots']['qmd']) / 'active.json'
    selected_bytes = selected_pointer.read_bytes()
    selected_pointer.write_text('{}\n')
    code, drift = cli('activate', root)
    assert code == 1 and drift == {'status': 'rejected',
        'reason': 'setup_pointer_changed_after_activation', 'changesApplied': 0}, drift
    assert cli('prepare', root, req)[1]['status'] == 'rejected'
    selected_pointer.write_bytes(selected_bytes)
    assert cli('activate', root)[1]['status'] == 'activated_unchanged'
    modified_same_generation = json.loads(selected_bytes)
    modified_same_generation['externalMarker'] = 'synthetic concurrent edit'
    selected_pointer.write_text(json.dumps(modified_same_generation))
    code, unsafe_rollback = cli('rollback', root)
    assert code == 1 and unsafe_rollback['status'] == 'recovery_required'
    assert unsafe_rollback['reason'] == 'setup_pointer_changed_after_activation'
    selected_pointer.write_bytes(selected_bytes)
    assert cli('activate', root)[1]['status'] == 'activated'
    assert runtime_update.select_runtime(root)['INDEX_PATH'] == first['staged']['index']['index']
    assert qmd_runtime.select(data['runtimeRoots']['qmd'])['generation'] == first['staged']['qmd']['generation']
    assert (root / '.auto-context/settings.json').is_file()
    assert all(p.read_bytes() == body for p, body in original.items())
    code, rolled = cli('rollback', root)
    assert code == 0 and rolled['status'] == 'rolled_back', rolled
    assert daemon_state.read_text().startswith('903\t')
    assert str(global_qmd) in daemon_state.read_text()
    assert cli('rollback', root)[1]['status'] == 'rolled_back_unchanged'
    assert not (root / '.auto-context/settings.json').exists()
    assert not (root / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert all(p.read_bytes() == body for p, body in original.items())

    # A completed transaction cannot permanently pin the next reviewed request.
    next_data = json.loads(json.dumps(data))
    next_data['index'] = {'mode': 'none'}
    next_req = base / 'legacy-next-request.json'
    next_req.write_text(json.dumps(next_data)); next_req.chmod(0o600)
    code, next_prepared = cli('prepare', root, next_req)
    assert code == 0 and next_prepared['status'] == 'prepared', next_prepared
    archive = root / '.auto-context/install-update-history'
    assert len(list(archive.glob('*.json'))) == 1
    assert cli('activate', root)[1]['status'] == 'activated'
    after_active = json.loads(json.dumps(next_data))
    after_active['qmd']['mode'] = 'active'
    after_active['qmd'].pop('qmd'); after_active['qmd'].pop('node')
    active_req = base / 'legacy-after-active-request.json'
    active_req.write_text(json.dumps(after_active)); active_req.chmod(0o600)
    code, active_replan = cli('prepare', root, active_req)
    assert code == 0 and active_replan['status'] == 'prepared', active_replan
    assert len(list(archive.glob('*.json'))) == 2
    assert cli('activate', root)[1]['status'] == 'activated'
    assert cli('rollback', root)[1]['status'] == 'rolled_back'
    assert all(p.read_bytes() == body for p, body in original.items())

    # Simulate process death at the exact post-link, pre-journal-settings-SHA point.
    crash_root, crash_config, crash_wiki, crash_cache, crash_old, _ = project('settings-crash')
    crash_req, crash_data = request(crash_root, crash_config, crash_wiki,
        crash_cache, crash_old, index_mode='none')
    assert cli('prepare', crash_root, crash_req)[1]['status'] == 'prepared'
    original_copy = setup._copy_legacy_settings
    def killed_after_copy(*args):
        original_copy(*args)
        raise KeyboardInterrupt('synthetic process death after settings link')
    setup._copy_legacy_settings = killed_after_copy
    try:
        try: cli('activate', crash_root)
        except KeyboardInterrupt: pass
        else: raise AssertionError('synthetic kill point not reached')
    finally:
        setup._copy_legacy_settings = original_copy
    crash_journal = cli('status', crash_root)[1]
    assert crash_journal['phase'] == 'activating'
    assert crash_journal['activation']['settingsIntent']['sha256'] == sha(crash_root / '.auto-context.json')
    assert (crash_root / '.auto-context/settings.json').is_file()
    assert cli('activate', crash_root)[1]['status'] == 'activated'
    assert cli('rollback', crash_root)[1]['status'] == 'rolled_back'
    assert not (crash_root / '.auto-context/settings.json').exists()
    assert (crash_root / '.auto-context.json').is_file()

    # Two host setup callers race on one reviewed request; one journal and generation win.
    concurrent, config, wiki, cache, old, _ = project('host-concurrent', legacy=False)
    concurrent_req, concurrent_data = request(concurrent, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    wrapper = base / 'setup-cli-fixture.py'
    wrapper.write_text('import sys\nsys.path.insert(0, ' +
        repr(str(repository / 'core')) + ')\nimport install_update as setup\n'
        'setup.supported_platform=lambda: True\nraise SystemExit(setup.main(sys.argv[1:]))\n')
    concurrent_env = {**os.environ, 'HOME': str(base / 'home-host-concurrent'),
        'PATH': str(global_bin) + os.pathsep + os.environ.get('PATH', ''),
        'QMD_BACKEND_MANAGER': str(manager), 'QMD_RECALL_LOG': '',
        'PYTHONDONTWRITEBYTECODE': '1'}
    commands = [sys.executable, str(wrapper), 'prepare', '--project', str(concurrent),
                '--request', str(concurrent_req)]
    first_host = subprocess.Popen(commands, env=concurrent_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second_host = subprocess.Popen(commands, env=concurrent_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    reports = []
    for process in (first_host, second_host):
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        reports.append(json.loads(stdout))
    assert all(row['status'] == 'prepared' for row in reports)
    assert reports[0]['staged']['qmd']['generation'] == reports[1]['staged']['qmd']['generation']
    qmd_generations = Path(concurrent_data['runtimeRoots']['qmd']) / 'generations'
    assert len(list(qmd_generations.iterdir())) == 1
    assert cli('rollback', concurrent)[1]['status'] == 'rolled_back'

    interrupted, config, wiki, cache, old, preserved = project('interrupted')
    req2, data2 = request(interrupted, config, wiki, cache, old)
    fail_embed[0] = True
    code, failed = cli('prepare', interrupted, req2)
    assert code == 1 and failed['status'] == 'failed_preparing', failed
    assert not (interrupted / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(data2['runtimeRoots']['qmd']) / 'active.json').exists()
    fail_embed[0] = False
    code, retried = cli('prepare', interrupted, req2)
    assert code == 0 and retried['status'] == 'prepared', retried
    original_activate = runtime_update.activate_shadow_index
    runtime_update.activate_shadow_index = lambda *_: (_ for _ in ()).throw(ValueError('synthetic_cutover_failure'))
    code, recovered = cli('activate', interrupted)
    runtime_update.activate_shadow_index = original_activate
    assert code == 1 and recovered['status'] == 'rolled_back_after_activation_failure', recovered
    assert not (interrupted / '.auto-context/settings.json').exists()
    assert not (Path(data2['runtimeRoots']['qmd']) / 'active.json').exists()
    assert not (interrupted / '.auto-context/qmd-index-active.json').exists()
    assert cli('prepare', interrupted, req2)[1]['status'] == 'prepared'
    assert cli('activate', interrupted)[1]['status'] == 'activated'
    assert cli('rollback', interrupted)[1]['status'] == 'rolled_back'

    legacy, config, wiki, cache, old, _ = project('old-wiki', v1=True)
    req3, data3 = request(legacy, config, wiki, cache, old, index_mode='none')
    code, held = cli('prepare', legacy, req3)
    assert code == 0 and held['status'] == 'awaiting_legacy_review', held
    assert cli('activate', legacy)[1]['status'] == 'awaiting_legacy_review'
    assert not (Path(data3['runtimeRoots']['qmd']) / 'active.json').exists()

    calls = []
    def fake_install(root, **kwargs):
        calls.append(kwargs)
        assert kwargs['allow_execution'] and kwargs['allow_lifecycle_scripts']
        return qmd_runtime.prepare_existing(root, qmd, node)
    qmd_installer.install_new = fake_install
    fresh, config, wiki, cache, old, _ = project('fresh', legacy=False)
    code, missing_v2 = entry_cli('prepare', fresh, '--qmd-mode', 'reuse',
        '--qmd-entry', qmd, '--node', node, '--qmd-config', config,
        '--model-cache', cache, '--source-index', old, '--approve-index-execution')
    assert code == 1 and missing_v2['status'] == 'blocked'
    assert 'published_v2_wiki_required' in missing_v2['blockers']
    assert not (fresh / '.auto-context/install-update-journal.json').exists()
    fake_npm = base / 'fake-npm';fake_npm.write_text('#!/bin/sh\nexit 99\n');fake_npm.chmod(0o700)
    code, runtime_plan = entry_cli('plan', fresh, '--qmd-mode', 'install',
        '--node', node, '--npm', fake_npm, '--index-mode', 'none')
    assert code == 1 and 'package_execution_approval_required' in runtime_plan['blockers']
    code, approved_install_plan = entry_cli('plan', fresh, '--qmd-mode', 'install',
        '--node', node, '--npm', fake_npm, '--index-mode', 'none',
        '--approve-package-execution', '--approve-lifecycle-scripts')
    assert code == 0 and approved_install_plan['status'] == 'ready_to_prepare'
    assert approved_install_plan['request']['qmd']['allowExecution'] is True
    assert not (fresh / '.auto-context/install-update-journal.json').exists()
    req4, data4 = request(fresh, config, wiki, cache, old, qmd_mode='install',
                           index_mode='none', config_mode='preserve')
    code, staged = cli('prepare', fresh, req4)
    assert code == 0 and staged['status'] == 'prepared' and len(calls) == 1, staged
    assert not (Path(data4['runtimeRoots']['qmd']) / 'active.json').exists()
    assert cli('activate', fresh)[1]['status'] == 'activated'
    assert cli('rollback', fresh)[1]['status'] == 'rolled_back'
    runtime_options = ('--qmd-mode', 'reuse', '--qmd-entry', qmd, '--node', node,
                       '--index-mode', 'none')
    code, runtime_only = entry_cli('prepare', fresh, *runtime_options)
    assert code == 0 and runtime_only['status'] == 'prepared', runtime_only
    assert runtime_only['migrationScope'] == 'runtime_only_no_v2_index'
    assert entry_cli('activate', fresh)[1]['status'] == 'activated'
    assert entry_cli('rollback', fresh)[1]['status'] == 'rolled_back'

    # Process death just after each pointer write must resume from journal intent.
    qmd_crash, config, wiki, cache, old, _ = project('qmd-pointer-crash', legacy=False)
    qmd_crash_req, qmd_crash_data = request(qmd_crash, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', qmd_crash, qmd_crash_req)[1]['status'] == 'prepared'
    qmd_activate_original = qmd_runtime.activate
    def killed_after_qmd(*args):
        result = qmd_activate_original(*args)
        raise KeyboardInterrupt('synthetic QMD pointer crash')
    qmd_runtime.activate = killed_after_qmd
    try:
        try: cli('activate', qmd_crash)
        except KeyboardInterrupt: pass
        else: raise AssertionError('QMD pointer kill point missed')
    finally: qmd_runtime.activate = qmd_activate_original
    assert cli('status', qmd_crash)[1]['activation']['intent'] == 'qmd'
    assert cli('activate', qmd_crash)[1]['status'] == 'activated'
    assert cli('rollback', qmd_crash)[1]['status'] == 'rolled_back'
    assert not (Path(qmd_crash_data['runtimeRoots']['qmd']) / 'active.json').exists()

    # An interrupted activation with a selected QMD pointer must not leave it
    # active when the next invocation's source precheck fails.
    resume_drift, config, wiki, cache, old, _ = project('resume-source-drift', legacy=False)
    resume_req, resume_data = request(resume_drift, config, wiki, cache, old,
        config_mode='preserve')
    assert cli('prepare', resume_drift, resume_req)[1]['status'] == 'prepared'
    qmd_runtime.activate = killed_after_qmd
    try:
        try: cli('activate', resume_drift)
        except KeyboardInterrupt: pass
        else: raise AssertionError('resume source drift kill point missed')
    finally: qmd_runtime.activate = qmd_activate_original
    assert (Path(resume_data['runtimeRoots']['qmd']) / 'active.json').is_file()
    modern = resume_drift / '.auto-context/settings.json'
    modern.write_text(modern.read_text() + ' ')
    code, resumed_drift = cli('activate', resume_drift)
    assert code == 1 and resumed_drift['status'] == 'rolled_back_after_activation_failure', resumed_drift
    assert resumed_drift['reason'] == 'project_sources_changed_during_setup'
    assert not (Path(resume_data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert not (resume_drift / '.auto-context/qmd-index-active.json').exists()
    assert cli('status', resume_drift)[1]['phase'] == 'rolled_back'

    # When a second writer changes the pointer after the interruption, the
    # precheck failure must report recovery_required and preserve those bytes.
    conflicted, config, wiki, cache, old, _ = project('resume-pointer-conflict', legacy=False)
    conflict_req, conflict_data = request(conflicted, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', conflicted, conflict_req)[1]['status'] == 'prepared'
    qmd_runtime.activate = killed_after_qmd
    try:
        try: cli('activate', conflicted)
        except KeyboardInterrupt: pass
        else: raise AssertionError('resume pointer conflict kill point missed')
    finally: qmd_runtime.activate = qmd_activate_original
    foreign_pointer = Path(conflict_data['runtimeRoots']['qmd']) / 'active.json'
    foreign_pointer.write_text('{}\n')
    foreign_pointer.chmod(0o600)
    modern = conflicted / '.auto-context/settings.json'
    modern.write_text(modern.read_text() + ' ')
    code, unsafe_resume = cli('activate', conflicted)
    assert code == 1 and unsafe_resume['status'] == 'recovery_required', unsafe_resume
    assert unsafe_resume['reason'] == 'project_sources_changed_during_setup'
    assert unsafe_resume['rollbackReason'] == 'setup_pointer_changed_after_activation'
    assert foreign_pointer.read_text() == '{}\n'
    assert cli('status', conflicted)[1]['phase'] == 'recovery_required'

    index_crash, config, wiki, cache, old, _ = project('index-pointer-crash', legacy=False)
    index_crash_req, _ = request(index_crash, config, wiki, cache, old,
        config_mode='preserve')
    assert cli('prepare', index_crash, index_crash_req)[1]['status'] == 'prepared'
    index_activate_original = runtime_update.activate_shadow_index
    def killed_after_index(*args):
        index_activate_original(*args)
        raise KeyboardInterrupt('synthetic index pointer crash')
    runtime_update.activate_shadow_index = killed_after_index
    try:
        try: cli('activate', index_crash)
        except KeyboardInterrupt: pass
        else: raise AssertionError('index pointer kill point missed')
    finally: runtime_update.activate_shadow_index = index_activate_original
    assert cli('status', index_crash)[1]['activation']['intent'] == 'index'
    assert cli('activate', index_crash)[1]['status'] == 'activated'
    assert cli('rollback', index_crash)[1]['status'] == 'rolled_back'
    assert not (index_crash / '.auto-context/qmd-index-active.json').exists()

    # The same recovery boundary applies after the index pointer was written.
    resume_index, config, wiki, cache, old, _ = project('resume-index-drift', legacy=False)
    resume_index_req, resume_index_data = request(resume_index, config, wiki, cache, old,
        config_mode='preserve')
    original_index_sha = sha(old)
    assert cli('prepare', resume_index, resume_index_req)[1]['status'] == 'prepared'
    runtime_update.activate_shadow_index = killed_after_index
    try:
        try: cli('activate', resume_index)
        except KeyboardInterrupt: pass
        else: raise AssertionError('resume index drift kill point missed')
    finally: runtime_update.activate_shadow_index = index_activate_original
    assert (resume_index / '.auto-context/qmd-index-active.json').is_file()
    (wiki / 'index.md').write_text('Changed while activation was interrupted.\n')
    code, index_drift = cli('activate', resume_index)
    assert code == 1 and index_drift['status'] == 'rolled_back_after_activation_failure', index_drift
    assert index_drift['reason'] == 'shadow_wiki_changed_after_prepare'
    assert not (resume_index / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(resume_index_data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert sha(old) == original_index_sha

    # A WAL commit changes SQLite's visible rows without changing main-file
    # bytes. Both first activation and a later retry must reject that drift.
    wal_drift, config, wiki, cache, old, _ = project('wal-source-drift', legacy=False)
    wal_writer = sqlite3.connect(old)
    assert wal_writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    wal_writer.execute('CREATE TABLE source_marker(body TEXT)')
    wal_writer.execute('INSERT INTO source_marker VALUES (?)', ('before prepare',))
    wal_writer.commit()
    wal_req, wal_data = request(wal_drift, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', wal_drift, wal_req)[1]['status'] == 'prepared'
    frozen_main = sha(old)
    wal_writer.execute('INSERT INTO source_marker VALUES (?)', ('committed after prepare',))
    wal_writer.commit()
    assert sha(old) == frozen_main and Path(str(old) + '-wal').exists()
    code, drifted = cli('activate', wal_drift)
    assert code == 1 and drifted['status'] == 'rejected', drifted
    assert drifted['reason'] == 'source_index_changed_during_setup'
    assert not (wal_drift / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(wal_data['runtimeRoots']['qmd']) / 'active.json').exists()
    wal_writer.close()
    assert sha(old) != frozen_main  # ordinary checkpoint, still changed content
    assert cli('activate', wal_drift)[1]['reason'] == 'source_index_changed_during_setup'

    rollback_wal, config, wiki, cache, old, _ = project('rollback-wal-drift', legacy=False)
    rollback_writer = sqlite3.connect(old)
    assert rollback_writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    rollback_writer.execute('CREATE TABLE source_marker(body TEXT)')
    rollback_writer.commit()
    rollback_req, _ = request(rollback_wal, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', rollback_wal, rollback_req)[1]['status'] == 'prepared'
    assert cli('activate', rollback_wal)[1]['status'] == 'activated'
    rollback_main = sha(old)
    rollback_writer.execute('INSERT INTO source_marker VALUES (?)', ('after cutover',))
    rollback_writer.commit()
    assert sha(old) == rollback_main
    code, rollback_drift = cli('rollback', rollback_wal)
    assert code == 1 and rollback_drift['status'] == 'recovery_required', rollback_drift
    assert rollback_drift['reason'] == 'source_index_changed_during_migration'
    assert (rollback_wal / '.auto-context/qmd-index-active.json').is_file()
    rollback_writer.close()

    # A checkpoint can change main-file bytes while preserving every visible
    # row. The same source snapshot remains eligible for cutover and rollback.
    stable, config, wiki, cache, old, _ = project('wal-checkpoint-same', legacy=False)
    stable_writer = sqlite3.connect(old)
    assert stable_writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    stable_writer.execute('CREATE TABLE source_marker(body TEXT)')
    stable_writer.execute('INSERT INTO source_marker VALUES (?)', ('unchanged',))
    stable_writer.commit()
    stable_req, _ = request(stable, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', stable, stable_req)[1]['status'] == 'prepared'
    stable_main = sha(old)
    stable_writer.close()
    assert sha(old) != stable_main
    checkpointed_main = sha(old)
    assert cli('activate', stable)[1]['status'] == 'activated'
    assert cli('rollback', stable)[1]['status'] == 'rolled_back'
    assert sha(old) == checkpointed_main  # guard did not checkpoint or rewrite main

    # A transaction that is uncommitted at preflight holds the SQLite writer
    # reservation. Cutover waits; after commit, its guarded proof sees drift.
    concurrent, config, wiki, cache, old, _ = project('wal-concurrent-cutover', legacy=False)
    concurrent_writer = sqlite3.connect(old)
    assert concurrent_writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    concurrent_writer.execute('CREATE TABLE source_marker(body TEXT)')
    concurrent_writer.commit()
    concurrent_req, _ = request(concurrent, config, wiki, cache, old, config_mode='preserve')
    prepared = cli('prepare', concurrent, concurrent_req)[1]
    assert prepared['status'] == 'prepared'
    concurrent_writer.execute('BEGIN IMMEDIATE')
    concurrent_writer.execute('INSERT INTO source_marker VALUES (?)', ('racing commit',))
    direct = subprocess.Popen([sys.executable, '-c',
        'import sys;sys.path.insert(0,"core");import runtime_update as r;'
        'r.activate_shadow_index(sys.argv[1],sys.argv[2])',
        str(concurrent), prepared['staged']['index']['generation']],
        cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(.2)
    assert direct.poll() is None and not (concurrent / '.auto-context/qmd-index-active.json').exists()
    concurrent_writer.commit()
    _, direct_error = direct.communicate(timeout=10)
    assert direct.returncode != 0 and 'source_index_changed_during_migration' in direct_error
    assert not (concurrent / '.auto-context/qmd-index-active.json').exists()
    concurrent_writer.close()

    # If activation was interrupted after publishing its pointer, a later WAL
    # commit makes resume fail closed instead of reporting activation success.
    resume_wal, config, wiki, cache, old, _ = project('resume-wal-drift', legacy=False)
    resume_writer = sqlite3.connect(old)
    assert resume_writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    resume_writer.execute('CREATE TABLE source_marker(body TEXT)')
    resume_writer.commit()
    resume_req, _ = request(resume_wal, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', resume_wal, resume_req)[1]['status'] == 'prepared'
    runtime_update.activate_shadow_index = killed_after_index
    try:
        try: cli('activate', resume_wal)
        except KeyboardInterrupt: pass
        else: raise AssertionError('resume WAL kill point missed')
    finally: runtime_update.activate_shadow_index = index_activate_original
    resume_writer.execute('INSERT INTO source_marker VALUES (?)', ('after pointer',))
    resume_writer.commit()
    code, resumed = cli('activate', resume_wal)
    assert code == 1 and resumed['status'] == 'recovery_required', resumed
    assert resumed['reason'] == 'source_index_changed_during_setup'
    assert resumed['rollbackReason'] == 'source_index_changed_during_migration'
    assert (resume_wal / '.auto-context/qmd-index-active.json').is_file()
    resume_writer.close()

    failed_daemon, config, wiki, cache, old, _ = project('daemon-reload-fails', legacy=False)
    failed_home = base / 'home-daemon-reload-fails'; failed_home.mkdir(mode=0o700)
    failed_state = failed_home / '.fake-daemon'
    failed_state.write_text('500\t' + str(node) + ' ' + str(global_qmd) +
                            ' mcp --http --port 19483\n')
    (failed_home / '.fail-next-reload').write_text('synthetic only\n')
    failed_req, failed_data = request(failed_daemon, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', failed_daemon, failed_req)[1]['status'] == 'prepared'
    code, failed_handoff = cli('activate', failed_daemon)
    assert code == 1 and failed_handoff['status'] == 'rolled_back_after_activation_failure', failed_handoff
    assert failed_handoff['reason'] == 'managed_daemon_reload_failed'
    assert not (Path(failed_data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert str(global_qmd) in failed_state.read_text()

    # A live but unhealthy daemon is still a handoff obligation. Its manager
    # identity returns code 3 with PID/command; activation must reload it.
    unhealthy, config, wiki, cache, old, _ = project('unhealthy-daemon', legacy=False)
    unhealthy_home = base / 'home-unhealthy-daemon'; unhealthy_home.mkdir(mode=0o700)
    unhealthy_state = unhealthy_home / '.fake-daemon'
    unhealthy_state.write_text('700\t' + str(node) + ' ' + str(global_qmd) +
                               ' mcp --http --port 19483\n')
    (unhealthy_home / '.unhealthy-daemon').write_text('synthetic unhealthy\n')
    unhealthy_req, _ = request(unhealthy, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', unhealthy, unhealthy_req)[1]['status'] == 'prepared'
    code, healed = cli('activate', unhealthy)
    assert code == 0 and healed['status'] == 'activated', healed
    assert not (unhealthy_home / '.unhealthy-daemon').exists()
    assert unhealthy_state.read_text().startswith('701\t')
    assert str(qmd) in unhealthy_state.read_text()
    assert cli('rollback', unhealthy)[1]['status'] == 'rolled_back'

    # A publisher holding the project wiki lock may write before cutover;
    # the actual CLI then sees the changed corpus and rejects the old DB.
    locked, config, wiki, cache, old, _ = project('publisher-cutover-race', legacy=False)
    locked_req, locked_data = request(locked, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', locked, locked_req)[1]['status'] == 'prepared'
    marker = base / 'publisher-lock-held'
    writer = subprocess.Popen([sys.executable, '-c',
        'import sys,time\n'
        'from pathlib import Path\n'
        'sys.path.insert(0,sys.argv[1])\n'
        'import wiki_compile as wc, wiki_mutation_lock as wl\n'
        'with wl.lock(Path(sys.argv[2])):\n'
        ' Path(sys.argv[3]).write_text("held")\n'
        ' time.sleep(.25)\n'
        ' assert wc.write_text_atomic(Path(sys.argv[4]),"Changed by actual compile writer.\\n")\n',
        str(Path.cwd() / 'core'), str(locked), str(marker), str(wiki / 'index.md')])
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
    assert marker.exists()
    actual_env = {**os.environ, 'HOME': str(base / ('home-' + locked.name)),
        'PATH': str(global_bin) + os.pathsep + os.environ.get('PATH', ''),
        'QMD_BACKEND_MANAGER': str(manager)}
    # Exercise the CLI lock on Linux CI too; the product platform gate is
    # tested separately below, and this synthetic wrapper only bypasses it.
    actual = subprocess.run([sys.executable, str(wrapper),
        'activate', '--project', str(locked)], env=actual_env,
        capture_output=True, text=True, timeout=20)
    writer.wait(timeout=5)
    assert writer.returncode == 0
    locked_result = json.loads(actual.stdout)
    assert actual.returncode == 1 and locked_result['status'] == 'rejected', (actual.stderr, locked_result)
    assert locked_result['reason'] == 'shadow_wiki_changed_after_prepare', locked_result
    assert not (locked / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(locked_data['runtimeRoots']['qmd']) / 'active.json').exists()

    # An uncoordinated manual edit after the pointer write still fails the
    # post-cutover hash check, and both pointers are immediately restored.
    late, config, wiki, cache, old, _ = project('manual-edit-at-cutover', legacy=False)
    late_req, late_data = request(late, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', late, late_req)[1]['status'] == 'prepared'
    original_activate_one = setup._activate_one
    def edit_after_index(root, auto, journal, kind):
        original_activate_one(root, auto, journal, kind)
        if kind == 'index': (wiki / 'index.md').write_text('Late manual edit.\n')
    setup._activate_one = edit_after_index
    try: code, late_result = cli('activate', late)
    finally: setup._activate_one = original_activate_one
    assert code == 1 and late_result['status'] == 'rolled_back_after_activation_failure', late_result
    assert late_result['reason'] == 'shadow_wiki_changed_after_prepare'
    assert not (late / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(late_data['runtimeRoots']['qmd']) / 'active.json').exists()

    # Timeout at each post-pointer subprocess boundary must return CLI JSON
    # and roll back immediately rather than escaping as a traceback.
    probe_timeout, config, wiki, cache, old, _ = project('qmd-probe-timeout', legacy=False)
    probe_req, probe_data = request(probe_timeout, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', probe_timeout, probe_req)[1]['status'] == 'prepared'
    original_route_check = setup._check_qmd_route
    def timed_out_route(_): raise subprocess.TimeoutExpired(['qmd', '--version'], 1)
    setup._check_qmd_route = timed_out_route
    try: code, probe_result = cli('activate', probe_timeout)
    finally: setup._check_qmd_route = original_route_check
    assert code == 1 and probe_result['status'] == 'rolled_back_after_activation_failure', probe_result
    assert 'timed out' in probe_result['reason']
    assert not (Path(probe_data['runtimeRoots']['qmd']) / 'active.json').exists()

    reload_timeout, config, wiki, cache, old, _ = project('daemon-reload-timeout', legacy=False)
    timeout_home = base / 'home-daemon-reload-timeout'; timeout_home.mkdir(mode=0o700)
    timeout_state = timeout_home / '.fake-daemon'
    timeout_state.write_text('800\t' + str(node) + ' ' + str(global_qmd) +
                             ' mcp --http --port 19483\n')
    timeout_req, timeout_data = request(reload_timeout, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    assert cli('prepare', reload_timeout, timeout_req)[1]['status'] == 'prepared'
    original_manager_command = setup._manager_command
    timeout_once = [True]
    def timed_out_reload(action):
        if action == 'reload' and timeout_once[0]:
            timeout_once[0] = False
            raise subprocess.TimeoutExpired(['backend_manager.sh', action], 120)
        return original_manager_command(action)
    setup._manager_command = timed_out_reload
    try: code, timeout_result = cli('activate', reload_timeout)
    finally: setup._manager_command = original_manager_command
    assert code == 1 and timeout_result['status'] == 'rolled_back_after_activation_failure', timeout_result
    assert 'timed out' in timeout_result['reason']
    assert not (Path(timeout_data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert str(global_qmd) in timeout_state.read_text()

    # SQLite may fail only at the final post-pointer read. This is an ordinary
    # activation error, not a traceback: restore the selected QMD and index.
    sqlite_fail, config, wiki, cache, old, _ = project('sqlite-post-cutover', legacy=False)
    sqlite_req, sqlite_data = request(sqlite_fail, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', sqlite_fail, sqlite_req)[1]['status'] == 'prepared'
    original_shadow_check = setup._check_shadow_documents
    def broken_final_sqlite(index_path, corpus, expected_model):
        if (sqlite_fail / '.auto-context/qmd-index-active.json').exists():
            raise sqlite3.OperationalError('synthetic post-cutover sqlite failure')
        return original_shadow_check(index_path, corpus, expected_model)
    setup._check_shadow_documents = broken_final_sqlite
    try: code, sqlite_result = cli('activate', sqlite_fail)
    finally: setup._check_shadow_documents = original_shadow_check
    assert code == 1 and sqlite_result['status'] == 'rolled_back_after_activation_failure', sqlite_result
    assert 'synthetic post-cutover sqlite failure' in sqlite_result['reason']
    assert not (sqlite_fail / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(sqlite_data['runtimeRoots']['qmd']) / 'active.json').exists()
    assert old.is_file()

    # If rollback itself cannot read SQLite, report recovery_required with
    # applied pointers intact; a later explicit rollback can still finish.
    recovery, config, wiki, cache, old, _ = project('sqlite-rollback-fails', legacy=False)
    recovery_req, recovery_data = request(recovery, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', recovery, recovery_req)[1]['status'] == 'prepared'
    original_shadow_check = setup._check_shadow_documents
    original_index_rollback = runtime_update.rollback_shadow_index
    def fail_final_read(index_path, corpus, expected_model):
        if (recovery / '.auto-context/qmd-index-active.json').exists():
            raise sqlite3.DatabaseError('synthetic final DB unreadable')
        return original_shadow_check(index_path, corpus, expected_model)
    def fail_rollback(_): raise sqlite3.OperationalError('synthetic rollback DB unavailable')
    setup._check_shadow_documents = fail_final_read
    runtime_update.rollback_shadow_index = fail_rollback
    try: code, recovery_result = cli('activate', recovery)
    finally:
        setup._check_shadow_documents = original_shadow_check
        runtime_update.rollback_shadow_index = original_index_rollback
    assert code == 1 and recovery_result['status'] == 'recovery_required', recovery_result
    assert 'synthetic final DB unreadable' in recovery_result['reason']
    assert 'synthetic rollback DB unavailable' in recovery_result['rollbackReason']
    assert cli('status', recovery)[1]['phase'] == 'recovery_required'
    assert (recovery / '.auto-context/qmd-index-active.json').is_file()
    assert (Path(recovery_data['runtimeRoots']['qmd']) / 'active.json').is_file()
    assert cli('rollback', recovery)[1]['status'] == 'rolled_back'
    assert not (recovery / '.auto-context/qmd-index-active.json').exists()
    assert not (Path(recovery_data['runtimeRoots']['qmd']) / 'active.json').exists()

    # A readable but unrelated DB and a wrong QMD config cannot be cut over.
    wrong_cfg, config, wiki, cache, old, _ = project('wrong-config', legacy=False)
    config.write_text('collections: {unrelated: {path: /tmp/unrelated}}\n')
    bad_req, _ = request(wrong_cfg, config, wiki, cache, old, config_mode='preserve')
    code, rejected_cfg = cli('prepare', wrong_cfg, bad_req)
    assert code == 1 and rejected_cfg['reason'] == 'shadow_qmd_config_wiki_missing'
    assert not (wrong_cfg / '.auto-context/qmd-index-active.json').exists()
    wrong_db, config, wiki, cache, old, _ = project('wrong-corpus', legacy=False)
    bad_db_req, _ = request(wrong_db, config, wiki, cache, old, config_mode='preserve')
    code, rejected_db = cli('prepare', wrong_db, bad_db_req)
    assert code == 1 and rejected_db['reason'] == 'shadow_wiki_documents_mismatch'
    assert not (wrong_db / '.auto-context/qmd-index-active.json').exists()
    stale, config, wiki, cache, old, _ = project('changed-wiki', legacy=False)
    stale_req, _ = request(stale, config, wiki, cache, old, config_mode='preserve')
    assert cli('prepare', stale, stale_req)[1]['status'] == 'prepared'
    (wiki / 'index.md').write_text('Changed after shadow preparation.\n')
    code, changed = cli('activate', stale)
    assert code == 1 and changed['reason'] == 'shadow_wiki_changed_after_prepare', changed
    assert not (stale / '.auto-context/qmd-index-active.json').exists()

    custom, config, wiki, cache, old, _ = project('custom-root', legacy=False)
    custom_req, custom_data = request(custom, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    custom_data['runtimeRoots']['qmd'] = str(base / 'unsupported-custom-qmd-root')
    custom_req.write_text(json.dumps(custom_data))
    code, custom_rejected = cli('prepare', custom, custom_req)
    assert code == 1 and custom_rejected['reason'] == 'qmd_runtime_root_must_be_default'
    assert not (custom / '.auto-context/install-update-journal.json').exists()

    # A selected managed QMD is reused without changing its pointer.
    already, config, wiki, cache, old, _ = project('already-managed', legacy=False)
    req_active, data_active = request(already, config, wiki, cache, old,
        qmd_mode='active', index_mode='none', config_mode='preserve')
    active_root = Path(data_active['runtimeRoots']['qmd'])
    existing = qmd_runtime.prepare_existing(active_root, qmd, node)
    qmd_runtime.activate(active_root, existing['generation'])
    active_bytes = (active_root / 'active.json').read_bytes()
    assert cli('prepare', already, req_active)[1]['status'] == 'prepared'
    assert cli('activate', already)[1]['status'] == 'activated'
    assert (active_root / 'active.json').read_bytes() == active_bytes
    assert cli('rollback', already)[1]['status'] == 'rolled_back'
    assert (active_root / 'active.json').read_bytes() == active_bytes

    # An older global QMD cannot be silently adopted as a managed generation.
    old_package = base / 'older-qmd'; (old_package / 'bin').mkdir(parents=True)
    (old_package / 'package.json').write_text('{"version":"2.4.0"}\n')
    older_qmd = old_package / 'bin/qmd'
    older_qmd.write_text('console.log("qmd 2.4.0");\n')
    older_qmd.chmod(0o700)
    old_runtime, config, wiki, cache, old, _ = project('old-qmd', legacy=False)
    req_old, data_old = request(old_runtime, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    data_old['qmd']['qmd'] = str(older_qmd)
    req_old.write_text(json.dumps(data_old))
    code, older_rejected = cli('prepare', old_runtime, req_old)
    assert code == 1 and older_rejected['status'] == 'failed_preparing'
    assert older_rejected['reason'] == 'qmd_runtime_incompatible'
    assert not (Path(data_old['runtimeRoots']['qmd']) / 'active.json').exists()

    # The legacy .agents config is copied only at activation and retained.
    agents, config, wiki, cache, old, _ = project('agents-config', legacy='agents')
    req_agents, data_agents = request(agents, config, wiki, cache, old,
        index_mode='none')
    agents_config = agents / '.agents/qmd-recall.json'
    agents_bytes = agents_config.read_bytes()
    assert cli('prepare', agents, req_agents)[1]['status'] == 'prepared'
    assert not (agents / '.auto-context/settings.json').exists()
    assert cli('activate', agents)[1]['status'] == 'activated'
    assert (agents / '.auto-context/settings.json').read_bytes() == agents_bytes
    assert cli('rollback', agents)[1]['status'] == 'rolled_back'
    assert agents_config.read_bytes() == agents_bytes
    assert not (agents / '.auto-context/settings.json').exists()

    reviewed, config, wiki, cache, old, _ = project('reviewed', legacy=False, v1=True)
    (reviewed / 'sources').mkdir()
    quote = 'A synthetic beacon opens at dawn.'
    (reviewed / 'sources/a.md').write_text(quote + '\n')
    req_review, data_review = request(reviewed, config, wiki, cache, old,
                                       config_mode='preserve')
    code, waiting = cli('prepare', reviewed, req_review)
    assert code == 0 and waiting['status'] == 'awaiting_legacy_review', waiting
    old_generation = waiting['staged']['index']['generation']
    revision = wiki_topical.source_snapshot(reviewed, 'sources/a.md', {})[0]
    span = {'sourcePath': 'sources/a.md', 'sourceRevisionSha256': revision['sha256'],
            'startLine': 1, 'endLine': 1, 'quoteAnchor': quote,
            'quoteSha256': wiki_topical.digest(quote.encode())}
    card = {'cardId': 'reviewed-beacon', 'title': 'Reviewed beacon', 'category': 'world-rule',
            'details': '', 'claims': [{'claimId': 'dawn-rule', 'statement': quote,
            'state': 'rule', 'timeScope': 'chapter-1', 'condition': 'at dawn', 'evidence': [span]}]}
    generated = topical_backend.stage_generation_response(reviewed,
        {'schema': wiki_topical.SCHEMA, 'cards': [card]})
    gid = generated['generationId']
    response = {'verdict': 'pass', 'checks': [{'claimId': 'dawn-rule',
        'sourcePath': 'sources/a.md', 'quoteSha256': span['quoteSha256'],
        'quoteAnchor': quote, 'supported': True}], 'reasons': []}
    proof = topical_backend.run_verification_backend(reviewed, gid, 'reviewed-beacon',
        {'extractor': {'builtins': ['codex']},
         'verify': {'builtins': ['codex'], 'crossEngine': 'off'}}, 'codex',
        allow_backend_execution=True, runner=lambda *_: (response, None, 0))
    assert proof['status'] == 'backend_pass'
    assert topical_publisher.publish(reviewed, gid, 'reviewed-beacon')['status'] == 'published_verified'
    old_v1 = wiki / 'v1.md'; old_v1_bytes = old_v1.read_bytes()
    data_review['wiki'] = {'mode': 'reviewed_v2', 'mappings': [{
        'legacyPath': old_v1.relative_to(reviewed).as_posix(),
        'legacySha256': sha(old_v1), 'generationId': gid, 'cardId': 'reviewed-beacon'}]}
    req_review.write_text(json.dumps(data_review))
    code, migrated = cli('prepare', reviewed, req_review)
    assert code == 0 and migrated['status'] == 'prepared', migrated
    assert migrated['staged']['index']['generation'] != old_generation
    code, reviewed_activated = cli('activate', reviewed)
    assert code == 0 and reviewed_activated['status'] == 'activated', reviewed_activated
    assert old_v1.read_bytes() == old_v1_bytes
    assert cli('rollback', reviewed)[1]['status'] == 'rolled_back'
    assert old_v1.read_bytes() == old_v1_bytes
    mapping_file = base / 'reviewed-entry-mappings.json'
    mapping_file.write_text(json.dumps(data_review['wiki']['mappings']));mapping_file.chmod(0o600)
    external_config = base / 'home-reviewed/.config/qmd/index.yml'
    external_config.parent.mkdir(parents=True, exist_ok=True)
    external_config.write_text('models:\n  embed: unrelated-global-model\n')
    entry_options = ('--qmd-mode', 'reuse', '--qmd-entry', qmd, '--node', node,
        '--qmd-config', config, '--model-cache', cache, '--source-index', old,
        '--reviewed-v2-mappings', mapping_file, '--approve-index-execution')
    code, auto_plan = entry_cli('plan', reviewed, *entry_options)
    assert code == 0 and auto_plan['status'] == 'ready_to_prepare', auto_plan
    assert auto_plan['request']['index']['expectedModel'] == 'synthetic-model'
    assert auto_plan['request']['index']['expectedDimension'] == 768
    assert auto_plan['request']['index']['config'] == str(config)
    assert auto_plan['request']['wiki'] == data_review['wiki']
    code, generated = entry_cli('prepare', reviewed, *entry_options)
    assert code == 0 and generated['status'] == 'prepared', generated
    generated_path = Path(generated['generatedRequest'])
    assert generated_path.is_file() and generated_path.stat().st_mode & 0o077 == 0
    assert json.loads(generated_path.read_text()) == auto_plan['request']
    assert entry_cli('activate', reviewed)[1]['status'] == 'activated'
    code, current_check = entry_cli('check-update', reviewed)
    assert code == 0 and current_check['status'] == 'update_review_required', current_check
    assert current_check['qmdReady'] and not current_check['overallReady'], current_check
    assert 'laya_managed_runtime_not_prepared' in current_check['reasons'], current_check
    assert entry_cli('rollback', reviewed)[1]['status'] == 'rolled_back'
    assert old_v1.read_bytes() == old_v1_bytes
    code, update_check = entry_cli('check-update', reviewed)
    assert code == 0 and update_check['status'] == 'update_review_required'
    assert 'project_index_not_selected' in update_check['reasons']
    generated_path.write_text(generated_path.read_text() + ' ')
    assert entry_cli('prepare', reviewed, *entry_options)[1]['reason'] == 'generated_request_changed'
    drift_data = json.loads(json.dumps(data_review))
    drift_data['config'] = 'copy_legacy'
    drift_req = base / 'reviewed-source-drift-request.json'
    drift_req.write_text(json.dumps(drift_data)); drift_req.chmod(0o600)
    assert cli('prepare', reviewed, drift_req)[1]['status'] == 'prepared'
    (reviewed / 'sources/a.md').write_text('A different synthetic beacon appears at dusk.\n')
    code, changed_source = cli('activate', reviewed)
    assert code == 1 and changed_source['status'] == 'rejected'
    assert not (reviewed / '.auto-context/qmd-index-active.json').exists()

    # The CLI also connects a prepared Laya generation, without uv/model calls.
    import context_learning.runtime_installer as laya_installer
    laya_original_stage = laya_installer.stage_generation
    laya_original_activate = laya_installer.activate_generation
    laya_calls = []
    laya_kill_after_pointer = [True]
    def fake_laya_stage(root, uv, uv_sha, lock, lock_sha):
        laya_calls.append('stage')
        root = Path(root); generation = root / 'generations/g-synthetic'
        generation.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        (root / 'generations').chmod(0o700)
        generation.chmod(0o700)
        executable = generation / 'venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.write_text('synthetic interpreter')
        executable.chmod(0o700)
        (generation / 'prepared.json').write_text(json.dumps({'schema_version': 1,
            'executable': str(executable), 'runtime_identity_sha256': 'a'*64,
            'uv_sha256': uv_sha, 'lock_sha256': lock_sha}))
        (generation / 'prepared.json').chmod(0o600)
        return {'generation': str(generation), 'executable': str(executable),
                'runtime_identity_sha256': 'a'*64}
    def fake_laya_activate(root, executable, adapter, model):
        laya_calls.append('activate')
        (Path(root) / 'active.json').write_text(json.dumps({'executable': executable}))
        (Path(root) / 'active.json').chmod(0o600)
        if laya_kill_after_pointer[0]:
            laya_kill_after_pointer[0] = False
            raise KeyboardInterrupt('synthetic Laya pointer crash')
        return {'status': 'activated'}
    laya_installer.stage_generation = fake_laya_stage
    laya_installer.activate_generation = fake_laya_activate
    with_laya, config, wiki, cache, old, _ = project('with-laya', legacy=False)
    req_laya, data_laya = request(with_laya, config, wiki, cache, old,
                                   index_mode='none', config_mode='preserve')
    for name in ('fake-uv', 'fake-lock', 'fake-adapter'):
        (base / name).write_text('synthetic')
    (base / 'fake-uv').chmod(0o700)
    (base / 'fake-model').mkdir()
    data_laya['laya'] = {'mode': 'install', 'uv': str(base / 'fake-uv'),
        'uvSha256': 'b'*64, 'lock': str(base / 'fake-lock'), 'lockSha256': 'c'*64,
        'adapter': str(base / 'fake-adapter'), 'modelDir': str(base / 'fake-model'),
        'allowExecution': True}
    req_laya.write_text(json.dumps(data_laya))
    code, laya_prepared = cli('prepare', with_laya, req_laya)
    assert code == 0 and laya_prepared['status'] == 'prepared', laya_prepared
    assert not (Path(data_laya['runtimeRoots']['laya']) / 'active.json').exists()
    try: cli('activate', with_laya)
    except KeyboardInterrupt: pass
    else: raise AssertionError('Laya pointer kill point missed')
    assert cli('status', with_laya)[1]['activation']['intent'] == 'laya'
    code, laya_activated = cli('activate', with_laya)
    assert code == 0 and laya_activated['status'] == 'activated', laya_activated
    assert laya_calls == ['stage', 'activate']
    assert cli('rollback', with_laya)[1]['status'] == 'rolled_back'
    assert not (Path(data_laya['runtimeRoots']['laya']) / 'active.json').exists()
    entry_install, _, _, _, _, _ = project('entry-laya-install', legacy=False)
    laya_options = ('--qmd-mode', 'reuse', '--qmd-entry', qmd, '--node', node,
        '--index-mode', 'none', '--laya-mode', 'install', '--uv', base / 'fake-uv',
        '--laya-lock', base / 'fake-lock', '--laya-adapter', base / 'fake-adapter',
        '--laya-model-dir', base / 'fake-model')
    code, denied_laya = entry_cli('plan', entry_install, *laya_options)
    assert code == 1 and 'laya_install_execution_approval_required' in denied_laya['blockers']
    approved_options = (*laya_options, '--approve-laya-install')
    code, laya_plan = entry_cli('plan', entry_install, *approved_options)
    assert code == 0 and laya_plan['request']['laya']['mode'] == 'install', laya_plan
    assert laya_plan['request']['laya']['uvSha256'] == sha(base / 'fake-uv')
    assert laya_plan['request']['laya']['lockSha256'] == sha(base / 'fake-lock')
    code, laya_entry_prepared = entry_cli('prepare', entry_install, *approved_options)
    assert code == 0 and laya_entry_prepared['status'] == 'prepared', laya_entry_prepared
    assert not (Path(laya_plan['request']['runtimeRoots']['laya']) / 'active.json').exists()
    assert entry_cli('activate', entry_install)[1]['status'] == 'activated'
    assert entry_cli('rollback', entry_install)[1]['status'] == 'rolled_back'
    laya_installer.stage_generation = laya_original_stage
    laya_installer.activate_generation = laya_original_activate

    # Existing Laya reuse is attested, but never changes the managed pointer.
    import context_learning.runtime_setup as laya_setup
    old_attest, old_choose = laya_setup.attest_runtime, laya_setup.choose_runtime
    reused_laya, config, wiki, cache, old, _ = project('reuse-laya', legacy=False)
    req_reuse, data_reuse = request(reused_laya, config, wiki, cache, old,
        index_mode='none', config_mode='preserve')
    laya_binary = base / 'existing-laya-python'; laya_binary.write_text('synthetic')
    laya_binary.chmod(0o700)
    data_reuse['laya'] = {'mode': 'reuse', 'executable': str(laya_binary),
        'adapter': str(base / 'fake-adapter'), 'modelDir': str(base / 'fake-model')}
    req_reuse.write_text(json.dumps(data_reuse))
    laya_setup.attest_runtime = lambda *_: {
        'attestation': {'base_model_sha256': 'd'*64},
        'probe': {'runtime_identity_sha256': 'e'*64}}
    laya_setup.choose_runtime = lambda **_: {'status': 'selected',
        'executable': str(laya_binary), 'runtime_identity_sha256': 'e'*64}
    assert cli('prepare', reused_laya, req_reuse)[1]['status'] == 'prepared'
    assert cli('activate', reused_laya)[1]['status'] == 'activated'
    assert not (Path(data_reuse['runtimeRoots']['laya']) / 'active.json').exists()
    assert cli('rollback', reused_laya)[1]['status'] == 'rolled_back'
    entry_reuse, _, _, _, _, _ = project('entry-laya-reuse', legacy=False)
    reuse_options = ('--qmd-mode', 'reuse', '--qmd-entry', qmd, '--node', node,
        '--index-mode', 'none', '--laya-mode', 'reuse', '--laya-executable', laya_binary,
        '--laya-adapter', base / 'fake-adapter', '--laya-model-dir', base / 'fake-model')
    code, reuse_plan = entry_cli('plan', entry_reuse, *reuse_options)
    assert code == 0 and reuse_plan['request']['laya']['mode'] == 'reuse', reuse_plan
    assert entry_cli('prepare', entry_reuse, *reuse_options)[1]['status'] == 'prepared'
    assert entry_cli('activate', entry_reuse)[1]['status'] == 'activated'
    assert entry_cli('rollback', entry_reuse)[1]['status'] == 'rolled_back'
    laya_setup.attest_runtime, laya_setup.choose_runtime = old_attest, old_choose

    unsupported, config, wiki, cache, old, _ = project('unsupported', legacy=False)
    req5, data5 = request(unsupported, config, wiki, cache, old,
                           index_mode='none', config_mode='preserve')
    setup.supported_platform = lambda: False
    assert cli('prepare', unsupported, req5)[1]['status'] == 'unsupported_managed_platform'
    assert not (unsupported / '.auto-context/install-update-journal.json').exists()
    print(json.dumps({'newInstallCli': True, 'existingReuseCli': True,
        'inactiveBeforeActivation': True, 'legacyConfigPreserved': True,
        'originalDbAndLearningPreserved': True, 'interruptedRetry': True,
        'activationFailureRolledBack': True, 'v1HeldForReview': True,
        'repeatedPrepareActivateRollback': True, 'reviewedV1ToV2Preserved': True,
        'managedLayaStageActivateRollback': True, 'managedQmdActiveReuse': True,
        'olderQmdRejected': True, 'agentsConfigPreserved': True,
        'activationFailureRetry': True, 'layaExplicitReuse': True,
        'liveWalCommitRejected': True, 'checkpointSameContentAccepted': True,
        'concurrentWalCommitBlocked': True, 'interruptedWalResumeFailsClosed': True,
        'rollbackWalDriftFailsClosed': True,
        'unsupportedNoMutation': True, 'generatedEntryNoAiRequest': True,
        'externalCalls': 0}))
