// The post-release smoke tooling (tests/support/smoke_fixture.py, scripts/release_smoke.py), in a fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { leftoverProcesses } from './runner.mjs';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';
import { scaled } from './timing.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

for (const module of ['tests.support.test_smoke_fixture', 'tests.support.test_release_smoke']) {
  test(`release smoke python ${module}`, () => {
    const fixture = fixtureHome('mvq');
    try {
      const r = spawnSync('python3', ['-B', '-m', 'unittest', module, '-v'],
        { cwd: ROOT, encoding: 'utf8', timeout: Math.ceil(scaled(300000)), env: fixtureEnv(fixture) });
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
}
