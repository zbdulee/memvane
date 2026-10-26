import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Laya local provider verifies explicit artifacts without loading a model', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-local-provider-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 16 tests/);
  assert.doesNotMatch(result.stderr, /skipped=/);
});
