import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('capture source diagnostics preserve the sample schema', { timeout: 180000 }, () => {
  const fixture = fixtureHome('mvl');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_context_learning_laya_route.SelectedRoute.test_capture_sources_join_request_id_and_do_not_change_the_sample',
      'memvane.learning.tests.integration.test_context_learning_laya_route.SelectedRoute.test_disabled_capture_has_no_source_event'],
    { encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
