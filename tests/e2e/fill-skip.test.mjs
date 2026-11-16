import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../support/temp.mjs';

test('completed content is skipped after a new head', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvf');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-v',
      '-s', 'memvane/store/tests/integration', '-t', '.', '-p', 'test_view_fill.py',
      '-k', 'completed_prefix_skips_content_revalidation_after_new_head'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
