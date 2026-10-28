import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('retroactive dedup scan queues near-duplicate cards from the selected laya-db', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-dedup-scan-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: { ...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 10 tests/);
});
