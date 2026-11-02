import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('independent priority3 review hypotheses use valid records and real product boundaries', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/priority3-review-regressions-checks.py'], {
    encoding: 'utf8', timeout: 240000,
    env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1',
      QMD_RECALL_LOG: '' },
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 12 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});
