import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('resident smoke fixture', {timeout: 30000}, () => {
  const fixture = fixtureHome('mvmodel');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.resident.tests.model.test_resident_embed_smoke'],
    {cwd: process.cwd(), encoding: 'utf8', timeout: 25000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
