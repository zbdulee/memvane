// Runs the journal domain's Python tests (unit and integration) in one process.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

test('journal domain python tests', () => {
  const r = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-s',
    join('memvane', 'journal', 'tests'), '-t', '.', '-p', 'test_*.py', '-v'],
  { cwd: ROOT, encoding: 'utf8', timeout: 600000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1' } });
  if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
  assert.ifError(r.error);
  assert.equal(r.signal, null);
  assert.equal(r.status, 0);
  assert.match(r.stderr, /\nOK\b/);
});
