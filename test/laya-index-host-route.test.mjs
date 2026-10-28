import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {copyFileSync, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

test('Laya host route selector keeps absent QMD and blocks stale or invalid selection', () => {
  const home = mkdtempSync(join(process.cwd(), 'laya-route-test-home-'));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-index-host-route-checks.py'], {
      encoding: 'utf8', timeout: 60000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '',
        QMD_SKIP_BACKGROUND_WORKER: '1', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 19 tests/);
  } finally { removeTemp(home); }
});

function makeProject() {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-host-route-')));
  const project = join(home, 'project');
  const auto = join(project, '.auto-context');
  mkdirSync(join(project, 'docs'), {recursive: true});
  mkdirSync(auto, {recursive: true});
  const settings = JSON.stringify({collections: ['docs'], indexing: true,
    collectionPaths: {docs: 'docs'}});
  writeFileSync(join(auto, 'settings.json'), settings);
  const modelId = 'synthetic-model';
  const embeddingFingerprint = 'a'.repeat(64);
  const dimension = 3;
  const vectorProfile = {
    profileId: createHash('sha256').update(JSON.stringify([
      modelId, embeddingFingerprint, dimension, 'cosine',
    ])).digest('hex'),
    modelId, embeddingFingerprint, dimension, metric: 'cosine',
    approvedProviderId: 'synthetic-provider',
    providerArtifactSha256: 'b'.repeat(64),
    providerRelativePath: 'core/synthetic_provider.py',
  };
  const route = {
    schema: 'laya-index-route-v1', status: 'active', projectRoot: project,
    settingsSha256: createHash('sha256').update(settings).digest('hex'),
    dbPath: join(project, 'index.sqlite'), queuePath: join(project, 'queue.sqlite'),
    dbIncarnation: '1'.repeat(32), scopeId: '6'.repeat(32),
    collectionName: 'docs', sourceRoot: join(project, 'docs'),
    pluginRoot: join(home, 'installed-laya'),
    journalPath: join(home, 'install-journal.sqlite'), installId: '3'.repeat(32),
    generation: `g-${'4'.repeat(32)}`,
    bundleManifestSha256: '2'.repeat(64), ownershipManifestSha256: '5'.repeat(64),
    vectorProfile,
  };
  return {home, project, auto, route};
}

test('Claude/Codex dispatch blocks present route before QMD setup or backend', () => {
  const {home, project, auto, route} = makeProject();
  const managerLog = join(home, 'manager.log');
  const manager = join(home, 'manager.sh');
  const legacyLog = join(home, 'legacy.log');
  const legacyIndex = join(home, 'legacy-index.py');
  writeFileSync(manager, `#!/bin/sh\necho called >> ${JSON.stringify(managerLog)}\n`, {mode: 0o755});
  writeFileSync(legacyIndex, `open(${JSON.stringify(legacyLog)}, 'w').write('called')\n`);
  const payload = JSON.stringify({hook_event_name: 'PostToolUse', cwd: project,
    tool_name: 'Write', tool_input: {file_path: join(project, 'docs', 'a.md')}});
  const env = {...process.env, HOME: home, QMD_SETUP_GUARD_FIXTURE: '1',
    QMD_RECALL_LOG: '', QMD_BACKEND_MANAGER: manager,
    QMD_CORE_INDEX_SCRIPT: legacyIndex, PYTHONDONTWRITEBYTECODE: '1'};
  const hook = join(process.cwd(), 'hooks', 'run-hook');
  const dispatch = () => spawnSync('bash', [hook, 'index', 'codex'], {
    cwd: home, input: payload, encoding: 'utf8', env, timeout: 10000,
  });
  try {
    writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
    const selected = dispatch();
    assert.equal(selected.status, 3, selected.stderr);
    assert.match(selected.stderr, /request refused/);
    assert.equal(existsSync(managerLog), false);
    assert.equal(existsSync(legacyLog), false);
    route.settingsSha256 = '0'.repeat(64);
    writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
    const stale = dispatch();
    assert.equal(stale.status, 3, stale.stderr);
    assert.equal(existsSync(managerLog), false);
    assert.equal(existsSync(legacyLog), false);
  } finally { removeTemp(home); }
});

test('selected recall refuses unproved install without entering QMD manager', () => {
  const {home, project, auto, route} = makeProject();
  const managerLog = join(home, 'manager.log');
  const manager = join(home, 'manager.sh');
  writeFileSync(manager, `#!/bin/sh\necho called >> ${JSON.stringify(managerLog)}\n`, {mode: 0o755});
  writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
  const hook = join(process.cwd(), 'hooks', 'run-hook');
  try {
    for (const engine of ['claude', 'codex']) {
      const result = spawnSync('bash', [hook, 'recall', engine], {
        cwd: home, input: JSON.stringify({cwd: project, prompt: 'find alpha in docs'}),
        encoding: 'utf8', timeout: 10000,
        env: {...process.env, HOME: home, TMPDIR: home, QMD_SETUP_GUARD_FIXTURE: '1',
          QMD_BACKEND_MANAGER: manager, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      });
      assert.equal(result.status, 3, result.stderr);
      assert.equal(result.stdout, '');
      assert.match(result.stderr, /request refused/);
      assert.equal(existsSync(managerLog), false);
      assert.deepEqual(readdirSync(home).filter((name) => name.startsWith('laya-route-payload.')), []);
    }
  } finally { removeTemp(home); }
});

test('all always-fired selected hook actions refuse unproved install without QMD', () => {
  const {home, project, auto, route} = makeProject();
  const managerLog = join(home, 'manager.log');
  const manager = join(home, 'manager.sh');
  writeFileSync(manager, `#!/bin/sh\necho called >> ${JSON.stringify(managerLog)}\n`, {mode: 0o755});
  writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
  const hook = join(process.cwd(), 'hooks', 'run-hook');
  const base = {cwd: project, hook_event_name: 'PostToolUse', tool_name: 'Write',
    tool_input: {file_path: join(project, 'docs', 'a.md')}};
  const actions = ['update-queued', 'gate', 'posttool', 'index', 'compile',
    'file-change-event'];
  try {
    for (const action of actions) {
      const payload = {...base, hook_event_name: action === 'gate' ? 'PreToolUse'
        : action === 'update-queued' ? 'SessionStart' : 'PostToolUse'};
      const result = spawnSync('bash', [hook, action, 'claude'], {
        cwd: home, input: JSON.stringify(payload), encoding: 'utf8', timeout: 10000,
        env: {...process.env, HOME: home, QMD_SETUP_GUARD_FIXTURE: '1',
          QMD_BACKEND_MANAGER: manager, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      });
      assert.equal(result.status, 3, `${action}: ${result.stderr}`);
      assert.match(result.stderr, /request refused/, action);
      assert.equal(existsSync(managerLog), false, action);
      assert.equal(result.stdout, '', action);
    }
  } finally { removeTemp(home); }
});

test('Hermes stops before QMD manager for present route and reports unavailable context', () => {
  const {home, project, auto, route} = makeProject();
  const fakePlugin = join(home, 'fake-plugin');
  const managerLog = join(home, 'manager.log');
  mkdirSync(join(fakePlugin, 'core'), {recursive: true});
  writeFileSync(join(fakePlugin, 'core', 'backend_manager.sh'),
    `#!/bin/sh\necho called >> ${JSON.stringify(managerLog)}\n`, {mode: 0o755});
  writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
  try {
    const code = `import json\nfrom hermes_adapter.core_bridge import recall_context, session_update, post_edit_sync\nprint(json.dumps(recall_context(user_message='find alpha', cwd=${JSON.stringify(project)})))\nsession_update(cwd=${JSON.stringify(project)})\npost_edit_sync(tool_name='write_file',args={'path':'docs/a.md','content':'x'},result='{}',status='ok',cwd=${JSON.stringify(project)})`;
    const result = spawnSync('python3', ['-B', '-c', code], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, HOME: home, PYTHONPATH: process.cwd(),
        QMD_HERMES_PLUGIN_ROOT_FOR_TEST: fakePlugin, QMD_RECALL_LOG: '',
        QMD_QUERY_FIXTURE: 'fixture.json', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(JSON.parse(result.stdout).context, /request refused/);
    assert.equal(existsSync(managerLog), false);
  } finally { removeTemp(home); }
});

test('malformed legacy input stays graceful while selected or stale routes block', () => {
  const {home, project, auto, route} = makeProject();
  const hook = join(process.cwd(), 'hooks', 'run-hook');
  const env = {...process.env, HOME: home, QMD_SETUP_GUARD_FIXTURE: '1',
    QMD_RECALL_LOG: '', QMD_QUERY_FIXTURE: 'fixture.json',
    QMD_CORE_RECALL_SCRIPT: '/bin/true', PYTHONDONTWRITEBYTECODE: '1'};
  const run = (action, input, cwd, extraEnv = {}) => spawnSync('bash', [hook, action, 'claude'], {
    input, cwd, encoding: 'utf8', timeout: 10000, env: {...env, ...extraEnv},
  });
  try {
    const legacy = run('recall', 'not json', project);
    assert.equal(legacy.status, 0, legacy.stderr);
    assert.equal(legacy.stdout, '');

    writeFileSync(join(auto, 'laya-index-route.json'), JSON.stringify(route));
    for (const input of ['not json', '']) {
      const selected = run('recall', input, project);
      assert.equal(selected.status, 3, selected.stderr);
      assert.match(selected.stderr, /request refused/);
    }
    const invalidCwd = run('recall', JSON.stringify({cwd: 'relative', prompt: 'find'}), home);
    assert.equal(invalidCwd.status, 3, invalidCwd.stderr);
  } finally { removeTemp(home); }
});

test('selected dispatch execs service in hook PID and unlinks payload before timeout', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'laya-exec-synthetic-')));
  try {
    mkdirSync(join(root, 'hooks'));
    mkdirSync(join(root, 'core'));
    copyFileSync(join(process.cwd(), 'hooks', 'run-hook'), join(root, 'hooks', 'run-hook'));
    writeFileSync(join(root, 'core', 'laya_index_host_route.py'), "print('candidate')\n");
    writeFileSync(join(root, 'core', 'laya_host_response.py'),
      'import os,time\nprint(os.getpid(),flush=True)\ntime.sleep(30)\n');
    const result = spawnSync('bash', [join(root, 'hooks', 'run-hook'), 'recall', 'claude'], {
      cwd: root, input: '{}', encoding: 'utf8', timeout: 900, killSignal: 'SIGTERM',
      env: {...process.env, HOME: root, TMPDIR: root, CLAUDE_PLUGIN_ROOT: root,
        QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.error?.code, 'ETIMEDOUT');
    assert.equal(result.stdout.trim(), String(result.pid));
    assert.deepEqual(readdirSync(root).filter((name) => name.startsWith('laya-route-payload.')), []);
  } finally { removeTemp(root); }
});
