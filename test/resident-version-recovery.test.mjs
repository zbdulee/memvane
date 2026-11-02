import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('hosted learning mismatches fail fast and load retries preserve the real recovery latch', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/resident-version-recovery-checks.py'], {
    encoding: 'utf8', timeout: 90000,
    env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1',
      QMD_RECALL_LOG: '' },
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 5 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
