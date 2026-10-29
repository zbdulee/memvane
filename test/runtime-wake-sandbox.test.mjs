import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';

test('runtime wakes written by a test never reach the user runtime root', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/runtime-wake-sandbox-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 6 tests/);
  assert.match(result.stderr, /\nOK\b/);
});

test('npm test gives every test process a temp runtime root', (t) => {
  if (process.env.npm_lifecycle_event !== 'test') {
    t.skip('only under npm test (the development guard exports the root)');
    return;
  }
  const root = process.env.MEMVANE_TEST_RUNTIME_ROOT;
  assert.ok(root, 'MEMVANE_TEST_RUNTIME_ROOT is not set');
  assert.ok(realpathSync(root).startsWith(realpathSync(tmpdir())), root);
});
