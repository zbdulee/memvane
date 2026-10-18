import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('stage C1 synthetic publication race and request identity fences', () => {
  const result = spawnSync('python3', ['test/fixtures/published-view-race.py'], {
    encoding: 'utf8', timeout: 15000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
