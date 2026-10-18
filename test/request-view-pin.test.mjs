import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('request view pin keeps one route/endpoint per request and fails closed on switches', () => {
  const result = spawnSync('python3', ['test/fixtures/request-view-pin-checks.py'], {
    encoding: 'utf8', timeout: 15000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
