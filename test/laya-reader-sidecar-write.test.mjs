import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';
test('managed sidecar document publication authority, cut and actual rollback', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-sidecar-write-')));
  const runtime = join(home, 'runtime');mkdirSync(runtime, { mode: 0o700 });
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-reader-sidecar-write-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 11 tests\b/);assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});
