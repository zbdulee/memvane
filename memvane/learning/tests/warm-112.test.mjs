import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../../../tests/support/temp.mjs';
test('slow synthetic resident completes warm through the actual entry', {timeout: 90000}, () => {
  const fixture = fixtureHome('mv112');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_warm_lifecycle_112'],
    {encoding:'utf8',timeout:85000,env:fixtureEnv(fixture)});
    process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal,null); assert.equal(r.status,0);
  } finally {removeTemp(fixture.home);}
});
