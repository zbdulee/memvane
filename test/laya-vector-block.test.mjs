import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('a vector fill stuck on one card is counted, named once per session and cleared', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-vector-block-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 8 tests/);
});
