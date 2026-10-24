import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('unused QA build abandonment preserves sources and fails closed on progress', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-unused-build-abandon-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /\nOK\b/);
});
