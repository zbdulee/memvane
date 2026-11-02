import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('project adoption and compile workers retain lock and queue identity across the move', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-project-dir-lock-race-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1',
      QMD_RECALL_LOG: '' },
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 5 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
