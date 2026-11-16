import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('enrollment pointer meets real warm and selector validation', { timeout: 180000 }, () => {
  const fixture = fixtureHome('mv111');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_enrolled_warm',
      'memvane.learning.tests.integration.test_scripts.ScriptChecks.test_project_learning_setup'],
    { encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture) });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
