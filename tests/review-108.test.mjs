import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from './support/temp.mjs';

test('1.0.8 review blockers', () => {
  const fixture = fixtureHome('mvr');
  try {
    const cases = [
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_large_semantic_deadline_preserves_lexical_recall',
      'memvane.runtime.tests.integration.test_host_bundle_handoff.HostBundleHandoff.test_background_unchanged_inventory_avoids_host_cli',
    ];
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v', ...cases],
      { env: fixtureEnv(fixture), encoding: 'utf8', timeout: 180000 });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
