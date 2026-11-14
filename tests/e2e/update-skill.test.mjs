// The update skill wrapper (skills/update/scripts/update.sh): its contract, the
// not-onboarded refusal and the sandbox no-op.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WRAPPER = join(ROOT, 'skills', 'update', 'scripts', 'update.sh');

function frontmatter(text) {
  const match = /^---\n([\s\S]*?)\n---/.exec(text);
  assert.ok(match, 'missing YAML frontmatter');
  return Object.fromEntries(match[1].split('\n').map((line) => {
    const idx = line.indexOf(':');
    return [line.slice(0, idx).trim(), line.slice(idx + 1).trim()];
  }));
}

test('update skill metadata and wrapper contract', () => {
  const skill = readFileSync(join(ROOT, 'skills', 'update', 'SKILL.md'), 'utf8');
  const meta = frontmatter(skill);
  assert.equal(meta.name, 'update');
  assert.match(meta.description, /sync/i);
  assert.match(meta.description, /memvane-db/);
  assert.match(skill, /setup skill/);

  const wrapper = readFileSync(WRAPPER, 'utf8');
  assert.match(wrapper, /SKILL_DIR=.*dirname/);
  assert.match(wrapper, /entry\/run\.py" classify update/);
  assert.match(wrapper, /run-hook" update "\$HOST_ENGINE"/);
  assert.match(wrapper, /current host is required/);
  assert.doesNotMatch(wrapper, /backend_manager|setup_guard|recall\.py|core\//);
  assert.ok((statSync(WRAPPER).mode & 0o111) !== 0, 'wrapper must be executable');
});

test('update wrapper names onboarding for a project without a selected route', () => {
  const fixture = fixtureHome('mvp');
  const dir = join(fixture.home, 'project');
  mkdirSync(join(dir, '.memvane'), { recursive: true });
  writeFileSync(join(dir, '.memvane', 'settings.json'), JSON.stringify({ name: 'demo', skipPaths: [] }));
  try {
    const result = spawnSync('bash', [WRAPPER, dir], {
      cwd: ROOT, encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture, { QMD_SANDBOX: '' }),
    });
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /setup skill/);
  } finally {
    removeTemp(fixture.home);
  }
});

test('update wrapper is a no-op in a sandbox', () => {
  const fixture = fixtureHome('mvp');
  try {
    const result = spawnSync('bash', [WRAPPER, fixture.home], {
      cwd: ROOT, encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture, { QMD_SANDBOX: '1' }),
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.equal(result.stdout, '');
  } finally {
    removeTemp(fixture.home);
  }
});
