import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('Laya dedup result reembeds keeper and omits loser while reusing other vectors', () => {
  const result = execFileSync('python3', ['-B', 'test/fixtures/laya_dedup_index_sync.py'], {
    encoding: 'utf8', env: { ...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '' },
  });
  assert.match(result, /Laya keeper edit, loser deletion and unchanged vector reuse passed/);
});
