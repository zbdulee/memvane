import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('host prompt recall keeps no learning capture', () => {
  const fixture = fixtureHome('mvh');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.host.tests.unit.test_capture_sources'],
    { encoding: 'utf8', timeout: 30000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
