import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

function fixture(name, count) {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-pending-recovery-checks.py', name], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, new RegExp(`Ran ${count} tests?`));
  assert.match(result.stderr, /\nOK\b/);
}

test('existing pending recovery refuses both branches after pinned caches disappear', () => {
  fixture('ExistingRecoveryBoundary', 1);
});

test('pending retarget changes only the CAS successor after proving a fresh bundle', () => {
  fixture('PendingRetargetTests', 11);
});
