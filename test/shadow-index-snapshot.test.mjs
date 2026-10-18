import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('C2a disposable WAL backup, candidate failure, and stale publication', () => {
  const result = spawnSync('python3', ['test/fixtures/shadow-index-snapshot-checks.py'], {
    encoding: 'utf8', timeout: 15000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
