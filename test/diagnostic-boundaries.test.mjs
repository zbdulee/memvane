import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('bundle-upgrade and Hermes recall failures link opt-in diagnostics without changing outcomes', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'memvane-diag-boundaries-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/diagnostic-boundaries-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, XDG_CONFIG_HOME: join(home, '.config'),
        QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
  } finally { removeTemp(home); }
});
