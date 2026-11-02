import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('candidate content fill uses acquired claims and complete immutable vector sets', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-blob-fill-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-blob-fill-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, XDG_STATE_HOME: join(home, 'state'),
        XDG_CACHE_HOME: join(home, 'cache'), XDG_CONFIG_HOME: join(home, 'config'),
        QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    assert.ifError(result.error);
    assert.equal(result.signal, null, result.stdout + result.stderr);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 31 tests in [\d.]+s\n\nOK\s*$/);
  } finally {
    removeTemp(home);
  }
});
