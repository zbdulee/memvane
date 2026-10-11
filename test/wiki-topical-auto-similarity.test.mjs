import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('exact-story semantic receipts replay, retry a recorded failure and invalidate changed pages', () => {
  const output = execFileSync('python3',
    ['test/fixtures/wiki-topical-auto-similarity-checks.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'}});
  assert.deepEqual(JSON.parse(output), {
    completedReceiptReused: true,
    changedPageInvalidated: true,
    failedAttemptRetriedOnce: true,
    duplicateHeld: true,
    uncertainNotRepeated: true,
    providerCalls: 0,
  });
});
