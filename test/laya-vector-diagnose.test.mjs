import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('a refused member embedding is named by the first provider rule its answer breaks', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-vector-diagnose-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 7 tests/);
});
