import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('QMD 2.5.3 and pinned fork migration adapter uses disposable fixtures', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-qmd-migration-checks.py'], {
    encoding: 'utf8', timeout: 45000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 31 tests/);
  assert.doesNotMatch(result.stderr, /skipped=/);
});
