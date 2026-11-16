import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('selector failures use bounded backoff and fixed startup reasons', () => {
  const fixture = fixtureHome('mvr111');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_selector_retry',
      'memvane.learning.tests.integration.test_selector_maintenance'],
    { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
