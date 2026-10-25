import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('setup entry defaults to durable inactive build and exposes its safe status flow', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-setup-entry-build-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 6 tests/);
});
