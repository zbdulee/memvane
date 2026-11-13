import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('host policy classifies recall and action decisions', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-host-rules-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 13 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
