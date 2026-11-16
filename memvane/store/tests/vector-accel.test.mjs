import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('Accelerate kernel and pinned store candidates', { timeout: 180000 }, () => {
  const fixture = fixtureHome('mva');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.store.tests.unit.test_vector_accel',
      'memvane.store.tests.integration.test_vector_accel'],
    { encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture) });
    process.stdout.write(r.stdout || ''); process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
