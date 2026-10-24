import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Laya exact plugin candidate and installer journal remain synthetic', () => {
  const run = spawnSync('python3', ['-B', 'test/fixtures/laya-product-bundle-checks.py'], {
    encoding: 'utf8', timeout: 90000,
    env: {...process.env, QMD_RECALL_LOG: '', QMD_REAL_MODEL_TESTS: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(run.status, 0,
    `python status=${run.status} signal=${run.signal} error=${run.error?.code ?? 'none'}\n${run.stdout}\n${run.stderr}`);
  assert.match(run.stderr, /Ran 17 tests/);
});
