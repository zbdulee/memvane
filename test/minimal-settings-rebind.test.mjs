import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

test('minimal settings migrate a selected Laya route with read-only recall and crash recovery', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'minimal-rebind-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/minimal-settings-rebind-checks.py'], {
      encoding: 'utf8', timeout: 180000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '', QMD_SKIP_BACKGROUND_WORKER: '1',
        PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 19 tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});
