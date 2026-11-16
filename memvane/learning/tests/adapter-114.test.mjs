import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('old cache namespaces rebind through entry before warm, profile and grant', {timeout: 180000}, () => {
  const fixture = fixtureHome('mva114');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_adapter_profile_114.AdapterProfile',
      'memvane.learning.tests.integration.test_context_learning_selector_heal_rebind',
      'memvane.learning.tests.integration.test_enrolled_warm',
      'memvane.learning.tests.integration.test_default_learning',
      'memvane.learning.tests.integration.test_settings_profile_113'],
    {encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
