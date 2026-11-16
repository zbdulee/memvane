import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('selector maintenance entry and deferral', () => {
  const fixture = fixtureHome('mvl');
  try {
    const suite = 'memvane.learning.tests.integration.test_selector_maintenance.SelectorMaintenance.';
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      suite + 'test_entry_binds_resident_starter_before_maintenance',
      suite + 'test_detached_entry_captures_bounded_stderr',
      suite + 'test_warm_starting_releases_retry_floor_as_deferral',
      suite + 'test_warm_translates_service_startup_and_deadline_to_deferral',
      suite + 'test_warm_failure_reports_fixed_code_and_stage',
      suite + 'test_entry_heals_warms_and_activates_with_different_python'],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
