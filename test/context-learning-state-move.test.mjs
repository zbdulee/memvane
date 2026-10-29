import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('learning state moves to MemVane and a stale selector heals itself', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/context-learning-state-move-checks.py'], {
    encoding: 'utf8', timeout: 240000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 34 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
