import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('conditional dirty queue ACK refuses a stale identity and keeps legacy ACK unchanged', () => {
  const p = spawnSync('python3', ['test/fixtures/dirty-queue-claim-identity-checks.py'], {
    encoding: 'utf8', timeout: 30000, env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 16 tests/);
});
