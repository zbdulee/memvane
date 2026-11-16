import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('setup runtime status', {timeout: 30000}, () => {
  const fixture = fixtureHome('mvsetup');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.app.tests.unit.test_setup_entry_build'],
    {cwd: process.cwd(), encoding: 'utf8', timeout: 25000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
