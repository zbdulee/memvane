import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('host bundle handoff', { timeout: 240000 }, () => {
  const fixture = fixtureHome('mvh');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.runtime.tests.integration.test_host_bundle_handoff'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 230000, env: fixtureEnv(fixture) });
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
