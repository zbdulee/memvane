import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('historically prepared engine bundles retain their manifest and owner proof', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-product-bundle-compatibility-checks.py'], {
    encoding: 'utf8', timeout: 90000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Ran 2 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
