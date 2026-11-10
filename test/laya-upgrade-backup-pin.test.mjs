import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('bundle upgrade backups: journal copy, store pins, retention and stage events', () => {
  const selected = process.env.MEMVANE_TEST_METHOD;
  const result = spawnSync(process.env.MEMVANE_TEST_PYTHON || 'python3',
    ['-B', 'test/fixtures/laya-upgrade-backup-pin-checks.py', ...(selected ? [selected] : [])], {
      encoding: 'utf8', timeout: 180000,
      env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
    });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, selected ? /Ran 1 test\b/ : /Ran 14 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});
