import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('automatic bundle upgrade: source, provider, host and status rules', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-bundle-auto-upgrade-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 13 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
