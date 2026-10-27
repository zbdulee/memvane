import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('selected dedup queues Laya only after deletion and retries failed wake', () => {
  const result = execFileSync('python3', ['-B', 'test/fixtures/laya_dedup_route.py'], {
    encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '' },
  });
  assert.match(result, /selected dedup retry and legacy queue isolation passed/);
});
