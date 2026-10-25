import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('host and setup keep installed plugin bytes exact without a test-only env', () => {
  const run = spawnSync('python3', ['-B', 'test/fixtures/laya-installed-bytecode-checks.py'], {
    encoding: 'utf8', timeout: 90000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(run.status, 0,
    `status=${run.status} signal=${run.signal} error=${run.error?.code ?? 'none'}\n${run.stdout}\n${run.stderr}`);
  assert.match(run.stderr, /Ran 2 tests/);
});
