import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../support/temp.mjs';

test('fill plans reuse targets only for the same head', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvp');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-v',
      '-s', 'memvane/store/tests/integration', '-t', '.', '-p', 'test_view_fill.py',
      '-k', 'head_bound_fill_plan_reuses_targets'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
