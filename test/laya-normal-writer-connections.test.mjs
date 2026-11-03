import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';
test('normal base and queue owners retain independent pins until native completion', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-normal-connections-')));
  const runtime = join(home, 'runtime'); mkdirSync(runtime, { mode: 0o700 });
  try {
    const r = spawnSync('python3', ['-B', 'test/fixtures/laya-normal-writer-connections-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    if (r.stdout) process.stdout.write(r.stdout);
    assert.ifError(r.error); assert.equal(r.status, 0, r.stdout+r.stderr);
    assert.match(r.stderr,/Ran 9 tests\b/); assert.match(r.stderr,/\nOK\b/);
  } finally { removeTemp(home); }
});
