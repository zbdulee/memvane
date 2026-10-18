import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('shadow publish flow ACKs only after a private pointer CAS', () => {
  const p = spawnSync('python3', ['test/fixtures/shadow-publish-flow-checks.py'], {
    encoding: 'utf8', timeout: 30000, env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 8 tests/);
});
