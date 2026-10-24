import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('multi collection source mapping keeps one sealed generation', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-multi-source-checks.py'], {
    encoding: 'utf8', timeout: 45000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 5 tests/);
});
