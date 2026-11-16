import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('unchanged inventory avoids repeated host CLI calls', () => {
  const fixture = fixtureHome('mvr');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.runtime.tests.integration.test_host_bundle_handoff.HostBundleHandoff.test_background_unchanged_inventory_avoids_host_cli'],
      { env: fixtureEnv(fixture), encoding: 'utf8', timeout: 180000 });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
