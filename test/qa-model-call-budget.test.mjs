import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('the optional QA model-call ceiling persists across worker processes', () => {
  const run = spawnSync('python3', ['test/fixtures/qa-model-call-budget-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1',
      QMD_SETUP_GUARD_FIXTURE: '1'},
  });
  assert.equal(run.status, 0, run.stdout + run.stderr);
});
