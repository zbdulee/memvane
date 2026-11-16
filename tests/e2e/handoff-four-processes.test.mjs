import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

test('two-bundle timer monitor resident worker handoff', {timeout: 150000}, () => {
  const fixture = fixtureHome('mvfour');
  try {
    const result = spawnSync('python3', ['-B', 'tests/e2e/handoff_four_processes.py'],
      {cwd:process.cwd(), encoding:'utf8', timeout:140000, env:fixtureEnv(fixture)});
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
