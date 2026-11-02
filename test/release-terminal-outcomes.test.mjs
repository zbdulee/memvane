import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('release completion preserves bound, inactive and downstream move outcomes', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/release-terminal-outcomes-checks.py'], {
    encoding: 'utf8', timeout: 90000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_RECALL_LOG: ''},
  });
  if (result.stdout) process.stdout.write(result.stdout);
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 7 tests\b/);
  assert.match(result.stderr, /\nOK\b/);
});
