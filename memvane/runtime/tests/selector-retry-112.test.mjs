import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('selector retries preserve edit and vector due in the real queue', {timeout: 90000}, () => {
  const fixture = fixtureHome('mvq112');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.runtime.tests.integration.test_selector_retry_queue_112'],
    {encoding: 'utf8', timeout: 85000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
