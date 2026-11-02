import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('transient resident start exhaustion recovers without weakening security failures', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-resident-recovery-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 7 tests/);
});
