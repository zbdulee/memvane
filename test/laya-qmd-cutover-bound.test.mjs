import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('bound initial-build cutover: bind, activate, prewrite restore, crash and proof fences', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-qmd-cutover-bound-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 23 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
