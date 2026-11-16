import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../support/temp.mjs';

test('large schema3 view profiles real fill with a synthetic provider', {timeout: 850000}, () => {
  const fixture = fixtureHome('mvs');
  try {
    const result = spawnSync('python3', ['-B', 'tests/e2e/sync_profile_scale.py'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 840000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
    assert.match(result.stdout, /SCALE_PROFILE /);
  } finally { removeTemp(fixture.home); }
});
