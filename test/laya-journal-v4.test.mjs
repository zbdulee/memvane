import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('journal v4 conversion and content-based provider re-proof', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-journal-v4-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 24 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
