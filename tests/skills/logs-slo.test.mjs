// The logs skill's service-level lines (tests/skills/logs_slo.py) in a fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

test('logs skill service-level lines: targets, floors, per-install blocks', () => {
  const fixture = fixtureHome('mvl');
  try {
    const r = spawnSync('python3', ['-B', join(ROOT, 'tests', 'skills', 'logs_slo.py')], {
      cwd: ROOT, encoding: 'utf8', timeout: 300000, env: fixtureEnv(fixture),
    });
    assert.ifError(r.error);
    assert.equal(r.signal, null, r.stderr);
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.match(r.stderr, /Ran \d+ tests/);
    assert.match(r.stderr, /\nOK\b/);
  } finally {
    removeTemp(fixture.home);
  }
});
