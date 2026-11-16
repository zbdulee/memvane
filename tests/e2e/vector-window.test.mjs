import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../support/temp.mjs';

test('ingest pass receives a vector window', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvw');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-v',
      '-s', 'memvane/sync/tests/integration', '-t', '.', '-p', 'test_schema3_vectors.py',
      '-k', 'vector_window'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
