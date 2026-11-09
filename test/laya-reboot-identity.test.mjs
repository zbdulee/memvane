import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('persisted install identities survive an APFS device reassignment', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-reboot-identity-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
