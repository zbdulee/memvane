import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('default learning enrollment and selector maintenance', { timeout: 300000 }, () => {
  const fixture = fixtureHome('mvl');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_default_learning',
      'memvane.learning.tests.integration.test_selector_maintenance',
      'memvane.learning.tests.unit.test_runtime_activation_entry',
      'memvane.learning.tests.integration.test_context_learning_profile',
      'memvane.learning.tests.integration.test_context_learning_project_worker'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture) });
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.signal, null);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
