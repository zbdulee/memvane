import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('wiki cards fit the local selector body budget: measure, compile gate, split, dedup', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-card-budget-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 32 tests/);
});
