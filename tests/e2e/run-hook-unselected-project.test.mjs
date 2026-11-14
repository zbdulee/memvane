// A project without a selected route through hooks/run-hook: one SessionStart
// onboarding notice, every other hook action silent, nothing written under HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const HOOK_ACTIONS = ['recall', 'update', 'update-queued', 'gate', 'posttool', 'index',
  'compile', 'file-change-event'];

function setup({ settings = true } = {}) {
  const fixture = fixtureHome('mvu');
  const project = join(fixture.home, 'project');
  mkdirSync(join(project, '.memvane'), { recursive: true });
  if (settings) {
    writeFileSync(join(project, '.memvane', 'settings.json'), JSON.stringify({ name: 'demo', skipPaths: [] }));
  } else {
    mkdirSync(join(project, 'docs'), { recursive: true });
  }
  return { fixture, project };
}

function hook(action, engine, fixture, project) {
  return spawnSync('bash', [join(ROOT, 'hooks', 'run-hook'), action, engine], {
    cwd: ROOT,
    input: JSON.stringify({ hook_event_name: 'SessionStart', cwd: project, prompt: '설정 파일은 어디에 있어?' }),
    encoding: 'utf8', timeout: 60000,
    env: fixtureEnv(fixture, { QMD_SANDBOX: '', CODEX_SANDBOX: '', CLAUDE_SANDBOX: '', CLAUDE_HEADLESS: '' }),
  });
}

/** Every path under `dir` with its size and mtime: a later write shows as a difference. */
function tree(dir, found = {}) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    const info = statSync(path);
    found[relative(dir, path) || '.'] = entry.isDirectory() ? 'dir' : `${info.size}:${info.mtimeMs}`;
    if (entry.isDirectory()) {
      for (const [rel, value] of Object.entries(tree(path))) found[join(entry.name, rel)] = value;
    }
  }
  return found;
}

test('a project without a selected route gets one SessionStart onboarding line and no other hook output', () => {
  const { fixture, project } = setup();
  try {
    const before = tree(fixture.home);
    const claude = hook('update-queued', 'claude', fixture, project);
    assert.equal(claude.status, 0, claude.stderr);
    // One short notice line, then one JSON line with the answer commands.
    const lines = claude.stdout.trim().split('\n');
    assert.equal(lines.length, 2, claude.stdout);
    assert.match(lines[0], /setup skill로 온보딩/);
    assert.equal(JSON.parse(lines[1].replace(/^\[memvane\] onboarding-answer /, '')).projectRoot, realpathSync(project));

    const codex = hook('update-queued', 'codex', fixture, project);
    assert.equal(codex.status, 0, codex.stderr);
    const event = JSON.parse(codex.stdout);
    assert.equal(event.hookSpecificOutput.hookEventName, 'SessionStart');
    assert.match(event.hookSpecificOutput.additionalContext, /setup skill로 온보딩/);

    for (const action of HOOK_ACTIONS.filter((name) => name !== 'update-queued')) {
      for (const engine of ['claude', 'codex']) {
        const result = hook(action, engine, fixture, project);
        assert.equal(result.status, 0, `${action} ${engine}: ${result.stderr}`);
        assert.equal(result.stdout, '', `${action} ${engine}`);
        assert.equal(result.stderr, '', `${action} ${engine}`);
      }
    }
    // No hook created index, cache, route or state files anywhere under HOME.
    assert.deepEqual(tree(fixture.home), before);
    assert.deepEqual(readdirSync(join(project, '.memvane')), ['settings.json']);
  } finally {
    removeTemp(fixture.home);
  }
});

test('a directory that never opted in stays silent at SessionStart', () => {
  const { fixture, project } = setup({ settings: false });
  try {
    const result = hook('update-queued', 'claude', fixture, project);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, '');
  } finally {
    removeTemp(fixture.home);
  }
});

test('hooks and skills no longer reach a QMD runtime, daemon or legacy recall', () => {
  const runHook = readFileSync(join(ROOT, 'hooks', 'run-hook'), 'utf8');
  for (const pattern of [/backend_manager/, /core\/recall\.py/, /core\/update\.sh/, /setup_guard/,
    /index_enqueue/, /preflight_gate/, /\.cache\/qmd/]) {
    assert.doesNotMatch(runHook, pattern);
  }
  for (const name of readdirSync(join(ROOT, 'memvane'))) {
    assert.doesNotMatch(name, /^qmd_|^shared_(install|qmd_setup)|^install_|^setup_guard|^recall\.py$|^backend_manager/, name);
  }
  assert.equal(existsSync(join(ROOT, 'backend')), false);
  assert.equal(existsSync(join(ROOT, 'core')), false);
  for (const skill of readdirSync(join(ROOT, 'skills'))) {
    const dir = join(ROOT, 'skills', skill, 'scripts');
    if (!existsSync(dir)) continue;
    for (const script of readdirSync(dir, { withFileTypes: true })) {
      if (!script.isFile()) continue;
      const text = readFileSync(join(dir, script.name), 'utf8');
      assert.doesNotMatch(text, /backend_manager|check-qmd|setup_guard|recall\.py|core\/update\.sh/, `${skill}/${script.name}`);
    }
  }
});
