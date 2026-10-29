import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('the project folder moves to .memvane/ while recall keeps serving', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-project-dir-move-checks.py'], {
    encoding: 'utf8', timeout: 300000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 26 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
