import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('single-character title retains searchable parity proof', () => {
  const result = spawnSync(process.env.MEMVANE_TEST_PYTHON || 'python3',
    ['-B', 'test/fixtures/laya-shadow-empty-query-checks.py'], {
      encoding: 'utf8', timeout: 300000,
      env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    });
  console.log(result.stdout);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 4 tests/);
});
