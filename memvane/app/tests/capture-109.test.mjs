import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('capture supervisor reports worker loss', () => {
  const fixture = fixtureHome('mva');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.app.tests.integration.test_capture_supervisor'],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
