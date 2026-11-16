import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('warm and resident compatibility', {timeout: 180000}, () => {
  const fixture = fixtureHome('mvc112');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_context_learning_selector_warm',
      'memvane.learning.tests.integration.test_context_learning_inference_service',
      'memvane.learning.tests.integration.test_learning_hosted_service_client',
      'memvane.learning.tests.integration.test_selector_maintenance',
      'memvane.learning.tests.integration.test_selector_retry'],
    {encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
