import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('out-of-project edits hint only an already-selected owner; the owner proves and compiles', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/out-of-project-edit-routing-checks.py'], {
    encoding: 'utf8', timeout: 300000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_SANDBOX: '1', QMD_SETUP_GUARD_FIXTURE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
