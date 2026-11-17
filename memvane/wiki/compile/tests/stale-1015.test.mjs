import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../../tests/support/temp.mjs';

test('stale compile backlog and productive pending results', { timeout: 240000 }, () => {
  const fixture = fixtureHome('mvstale');
  try {
    const result = spawnSync(process.env.PYTHON || 'python3', ['-B', '-m', 'unittest', '-v',
      'memvane.wiki.compile.tests.integration.test_stale_backlog_1015'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 230000, env: fixtureEnv(fixture) });
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.signal, null);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
