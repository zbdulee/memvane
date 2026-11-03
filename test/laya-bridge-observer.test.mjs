import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('bridge observer owns synthetic launch while kernel peer and transport stay real', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-observer-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, { mode: 0o700 });
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-bridge-observer-checks.py'], {
      encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 11 tests\b/);
    assert.match(result.stderr, /\nOK\b/);
  } finally {
    removeTemp(home);
  }
});
