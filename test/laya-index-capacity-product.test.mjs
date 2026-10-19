import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('capacity sync_step and query_bound bind, stream, recover and fail closed', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-index-capacity-product-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 24 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
