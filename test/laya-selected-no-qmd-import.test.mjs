import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('selected Laya hook, onboarding, sync and compile flows never load QMD modules', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-selected-no-qmd-import-checks.py'], {
    encoding: 'utf8', timeout: 1800000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 2 tests/);
});
