import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('diagnostic capture is opt-in, bounded, and detects incomplete records', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'memvane-diag-capture-')));
  try {
    const r = spawnSync('python3', ['-B', 'test/fixtures/diagnostic-capture-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config'),
        QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(r.status, 0, r.stdout + r.stderr);
  } finally { removeTemp(home); }
});
