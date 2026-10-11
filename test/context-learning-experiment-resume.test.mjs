import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('synthetic experiment retries only uncertain calls with a new counted key', () => {
  const run = spawnSync('python3', ['test/fixtures/context-learning-experiment-resume-checks.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(run.status, 0, run.stderr || run.stdout);
  assert.deepEqual(JSON.parse(run.stdout), { status: 'ok' });
});
