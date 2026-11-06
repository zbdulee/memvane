import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('publication cut identity compares every field across sidecar seal states', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-cut-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 3 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
