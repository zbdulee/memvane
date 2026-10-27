import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

test('QMD-origin detach and settings-identity conversion keep recall at every step', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'origin-detach-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-origin-detach-checks.py'], {
      encoding: 'utf8', timeout: 300000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '', QMD_SKIP_BACKGROUND_WORKER: '1',
        PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 17 tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});
