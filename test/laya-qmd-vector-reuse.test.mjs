import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('QMD-origin vector reuse needs exact chunk identity and sampled parity', () => {
  const run = spawnSync('python3', ['-B', 'test/fixtures/laya-qmd-vector-reuse-checks.py'], {
    encoding: 'utf8', timeout: 30000, env: {...process.env, QMD_RECALL_LOG: ''}});
  assert.equal(run.status, 0, run.stdout + run.stderr);
  assert.match(run.stderr, /Ran 4 tests/);
});
