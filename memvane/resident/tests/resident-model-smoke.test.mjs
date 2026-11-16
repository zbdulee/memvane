import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('isolated real resident embed worker', { timeout: 300000 }, () => {
  const result = spawnSync('python3', ['-B', 'memvane/resident/tests/model/resident_embed_smoke.py'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  process.stdout.write(result.stdout || '');
  process.stderr.write(result.stderr || '');
  assert.ifError(result.error);
  assert.equal(result.signal, null);
  assert.equal(result.status, 0);
});
