import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';

test('interrupted enrollment recovers before profile and grant activation', {timeout: 180000}, () => {
  const fixture = fixtureHome('mvr115');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_recovery_115'],
    {encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.signal, null);
    assert.equal(result.status, 0);
  } finally {removeTemp(fixture.home);}
});
