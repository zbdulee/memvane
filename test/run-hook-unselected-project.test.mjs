import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const HOOK_ACTIONS = ['recall', 'update', 'update-queued', 'gate', 'posttool', 'index',
  'compile', 'file-change-event', 'topical-event', 'topical-stop', 'topical-reconcile'];

function setup({ settings = true } = {}) {
  const home = mkdtempSync(join(tmpdir(), 'qmd-unselected-'));
  const project = join(home, 'project');
  mkdirSync(join(project, '.auto-context'), { recursive: true });
  if (settings) {
    writeFileSync(join(project, '.auto-context', 'settings.json'),
      JSON.stringify({ name: 'demo', skipPaths: [] }));
  } else {
    mkdirSync(join(project, 'docs'), { recursive: true });
  }
  return { home, project };
}

function hook(action, engine, home, project) {
  return spawnSync('bash', ['hooks/run-hook', action, engine], {
    input: JSON.stringify({ hook_event_name: 'SessionStart', cwd: project, prompt: '설정 파일은 어디에 있어?' }),
    encoding: 'utf8', timeout: 30000,
    env: { ...process.env, HOME: home, QMD_SANDBOX: '', CODEX_SANDBOX: '', CLAUDE_SANDBOX: '',
      CLAUDE_HEADLESS: '', QMD_RECALL_LOG: '' },
  });
}

test('a project without a selected route gets one SessionStart onboarding line and no other hook output', () => {
  const { home, project } = setup();
  try {
    const claude = hook('update-queued', 'claude', home, project);
    assert.equal(claude.status, 0, claude.stderr);
    const lines = claude.stdout.trim().split('\n');
    assert.equal(lines.length, 1, claude.stdout);
    assert.match(lines[0], /setup skill로 온보딩/);
    assert.ok(lines[0].includes(project));

    const codex = hook('update-queued', 'codex', home, project);
    assert.equal(codex.status, 0, codex.stderr);
    const event = JSON.parse(codex.stdout);
    assert.equal(event.hookSpecificOutput.hookEventName, 'SessionStart');
    assert.match(event.hookSpecificOutput.additionalContext, /setup skill로 온보딩/);

    for (const action of HOOK_ACTIONS.filter((name) => name !== 'update-queued')) {
      for (const engine of ['claude', 'codex']) {
        const result = hook(action, engine, home, project);
        assert.equal(result.status, 0, `${action} ${engine}: ${result.stderr}`);
        assert.equal(result.stdout, action === 'topical-stop' ? '{}\n' : '', `${action} ${engine}`);
        assert.equal(result.stderr, '', `${action} ${engine}`);
      }
    }
    // No hook created index, cache or QMD state anywhere under HOME.
    assert.deepEqual(readdirSync(home).sort(), ['project']);
    assert.deepEqual(readdirSync(join(project, '.auto-context')), ['settings.json']);
  } finally {
    removeTemp(home);
  }
});

test('a directory that never opted in stays silent at SessionStart', () => {
  const { home, project } = setup({ settings: false });
  try {
    const result = hook('update-queued', 'claude', home, project);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, '');
  } finally {
    removeTemp(home);
  }
});

test('hooks and skills no longer reach a QMD runtime, daemon or legacy recall', () => {
  const runHook = readFileSync('hooks/run-hook', 'utf8');
  for (const pattern of [/backend_manager/, /core\/recall\.py/, /core\/update\.sh/, /setup_guard/,
    /index_enqueue/, /preflight_gate/, /\.cache\/qmd/]) {
    assert.doesNotMatch(runHook, pattern);
  }
  for (const name of readdirSync('core')) {
    assert.doesNotMatch(name, /^qmd_|^shared_(install|qmd_setup)|^install_|^setup_guard|^recall\.py$|^backend_manager/, name);
  }
  assert.equal(existsSync('backend'), false);
  for (const skill of readdirSync('skills')) {
    const dir = join('skills', skill, 'scripts');
    if (!existsSync(dir)) continue;
    for (const script of readdirSync(dir)) {
      const text = readFileSync(join(dir, script), 'utf8');
      assert.doesNotMatch(text, /backend_manager|check-qmd|setup_guard|recall\.py|core\/update\.sh/, `${skill}/${script}`);
    }
  }
});

test('wakes of retired QMD kinds are acknowledged instead of retried forever', () => {
  const result = spawnSync('python3', ['-B', '-c', `
import json, sys
sys.path.insert(0, 'core')
import runtime_handlers
print(json.dumps([runtime_handlers.dispatch({'kind': kind, 'target': '/nonexistent'})
                  for kind in ('index', 'update', 'cleanup')]))
`], { encoding: 'utf8', env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1' } });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), [true, true, true]);
});
