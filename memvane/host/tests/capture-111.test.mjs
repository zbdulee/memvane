import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('capture admission bounds the supervisor tree', () => {
  const fixture = fixtureHome('mvc111');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.host.tests.unit.test_capture_budget'],
    { encoding: 'utf8', timeout: 30000, env: fixtureEnv(fixture) });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
