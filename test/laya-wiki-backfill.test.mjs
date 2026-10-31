import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('selected initial wiki backfill is finite, capped, revision-bound and acknowledged by the worker', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-wiki-backfill-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 27 tests/);
});
