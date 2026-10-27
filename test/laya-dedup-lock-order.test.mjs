import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('dedup waits for compiler/verifier/retirement card lock', () => {
  const result = execFileSync('python3', ['-B', 'test/fixtures/laya_dedup_lock_order.py'], {
    encoding: 'utf8', timeout: 15000,
    env: { ...process.env, QMD_RECALL_LOG: '', QMD_SETUP_GUARD_FIXTURE: '1' },
  });
  assert.match(result, /dedup waits for the shared CUD card-write lock/);
});
