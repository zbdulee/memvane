import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('timer, monitor, resident and wake handoff', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvhandoff');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.runtime.tests.integration.test_runtime_wake_queue',
      'memvane.runtime.tests.integration.test_runtime_monitor'],
    {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
