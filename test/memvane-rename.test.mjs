import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('memvane plugin-id switch: adoption, 0.40.3 route fence and the version rule', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/memvane-rename-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 10 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
