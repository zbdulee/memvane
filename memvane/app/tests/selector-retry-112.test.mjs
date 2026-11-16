import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('learning handler composes maintenance and ordinary learning', {timeout: 90000}, () => {
  const fixture = fixtureHome('mva112');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.app.tests.integration.test_wake_handlers',
      'memvane.app.tests.unit.test_runtime_handlers'],
    {encoding: 'utf8', timeout: 85000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
