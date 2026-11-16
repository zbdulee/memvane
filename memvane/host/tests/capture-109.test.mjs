import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('capture is dispatched after output', () => {
  const fixture = fixtureHome('mvh');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.host.tests.integration.test_host_response.HookEntry.test_capture_runs_after_the_answer_is_flushed_and_never_changes_it',
      'memvane.host.tests.integration.test_host_response.HookEntry.test_capture_dispatch_uses_owned_child_and_records_spawn_failure',
      'memvane.host.tests.integration.test_host_response.HookEntry.test_capture_child_entry_records_disabled_outcome'],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
