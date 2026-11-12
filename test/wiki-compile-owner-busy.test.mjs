import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

test('compile owner contention defers without consuming wake attempts', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'compile-owner-busy-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, {mode: 0o700});
  try {
    const child = spawnSync('python3', ['-B', 'test/fixtures/wiki-compile-owner-busy-checks.py'], {
      encoding: 'utf8',
      env: {...process.env, HOME: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SANDBOX: '', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
    });
    if (child.stdout) process.stdout.write(child.stdout);
    if (child.stderr) process.stderr.write(child.stderr);
    assert.ifError(child.error);
    assert.equal(child.status, 0, `fixture failed with ${child.status}`);
  } finally {
    removeTemp(home);
  }
});
