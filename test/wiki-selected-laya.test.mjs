import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('selected wiki compile route and local retrieval are synthetic', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-selected-laya-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', QMD_SANDBOX: '1',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 13 tests/);
});
