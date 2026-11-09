import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('diagnostic targets: owner registry register/resolve, file safety, journal recovery', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'memvane-diag-targets-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/diagnostic-targets-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config'),
            PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran \d+ tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally {
    removeTemp(home);
  }
});
