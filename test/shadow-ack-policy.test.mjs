import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('shadow ACK retry policy ACKs only an exact sealed CAS for the same claim', () => {
  const p = spawnSync('python3', ['test/fixtures/shadow-ack-policy-checks.py'], {
    encoding: 'utf8', timeout: 30000, env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 16 tests/);
});
