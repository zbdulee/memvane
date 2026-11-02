import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('fenced active recovery preserves snapshots across crashes and a real journal CAS', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-active-recovery-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 9 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
