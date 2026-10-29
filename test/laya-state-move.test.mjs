import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('install state moves to the MemVane base while recall keeps serving', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-state-move-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 27 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
