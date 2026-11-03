import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('GB6 preserves actual corruption and checkout refusals across elapsed deadlines', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-review-home-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, { mode: 0o700 });
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-view-review-regressions-checks.py'], {
      encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        QMD_CACHE_DIR: join(home, 'cache'), QMD_SETUP_GUARD_FIXTURE: '1',
        PYTHONDONTWRITEBYTECODE: '1' },
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 8 tests in /);
    assert.match(result.stderr, /\nOK\s*$/);
  } finally {
    removeTemp(home);
  }
});
