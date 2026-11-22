// The learning state model stays data: its document is rendered from it and no hook path imports it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };

test('docs/dev/learning-state.md is the rendered state model', () => {
  const r = spawnSync('python3', ['-B', join(ROOT, 'scripts', 'render_learning_state.py'), '--check'],
    { cwd: ROOT, encoding: 'utf8', timeout: 60000, env });
  assert.equal(r.status, 0, r.stderr);
});

test('the state model imports nothing and only tests and the renderer import it', () => {
  const model = readFileSync(join(ROOT, 'memvane/learning/context_learning/state_model.py'), 'utf8');
  assert.doesNotMatch(model, /^\s*(import|from)\s/m);
  const users = spawnSync('git', ['-C', ROOT, 'grep', '-l', 'state_model', '--', '*.py', '*.mjs'],
    { encoding: 'utf8', timeout: 60000 }).stdout.split('\n').filter(Boolean)
    .filter((file) => file !== 'memvane/learning/context_learning/state_model.py');
  assert.ok(users.length > 0);
  for (const file of users) {
    assert.ok(file.split('/').includes('tests') || file === 'scripts/render_learning_state.py', file);
  }
});
