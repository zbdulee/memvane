import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('shared settings reach warm, profile and grant through entry', {timeout: 180000}, () => {
  const fixture = fixtureHome('mvs113');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_settings_profile_113',
      'memvane.learning.tests.integration.test_context_learning_profile',
      'memvane.learning.tests.integration.test_default_learning',
      'memvane.learning.tests.integration.test_scripts.ScriptChecks.test_context_learning_project_scope',
      'memvane.learning.tests.integration.test_scripts.ScriptChecks.test_project_learning_setup'],
    {encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
