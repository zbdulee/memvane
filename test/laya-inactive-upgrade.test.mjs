import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('inactive candidate bundle switch, interruption and rollback are fenced', () => {
  const selected = process.env.MEMVANE_TEST_METHOD;
  const result = spawnSync(process.env.MEMVANE_TEST_PYTHON || 'python3', ['test/fixtures/laya-inactive-upgrade-checks.py', ...(selected ? [selected] : [])], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, selected ? /Ran 1 test\b/ : /Ran 8 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});
