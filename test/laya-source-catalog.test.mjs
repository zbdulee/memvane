import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('step-bounded source catalog publishes complete snapshots and serves only full byte proofs', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-source-catalog-checks.py'], {
    encoding: 'utf8', timeout: 300000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 18 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
