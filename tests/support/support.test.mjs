// The shared fixtures in tests/support (test_support.py), in a fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { leftoverProcesses } from './runner.mjs';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

test('tests/support fixtures', () => {
  const fixture = fixtureHome('mvs');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', 'tests.support.test_support', '-v'],
      { cwd: ROOT, encoding: 'utf8', timeout: 600000, env: fixtureEnv(fixture) });
    if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
    assert.ifError(r.error);
    assert.equal(r.signal, null);
    assert.equal(r.status, 0);
    assert.match(r.stderr, /\nOK\b/);
    assert.deepEqual(leftoverProcesses(fixture.home), []);
  } finally {
    removeTemp(fixture.home);
  }
});
