import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('inactive WAL backup and private pointer CAS fence candidate and late worker', () => {
  const p = spawnSync('python3', ['test/fixtures/shadow-index-adapter-checks.py'], {
    encoding: 'utf8', timeout: 30000, env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 2 tests/);
});
