import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('capacity and CRUD fixture admission isolation has explicit product controls', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/priority3-admission-fixture-checks.py'], {
    encoding: 'utf8', timeout: 240000,
    env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
      QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 6 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});
