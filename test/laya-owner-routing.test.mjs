import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('selected Laya hook wakes only a proved, approved Laya owner of an outside edit and fails closed otherwise', () => {
  // Import-time HOME constants must never point at the developer's real HOME.
  const home = mkdtempSync(join(process.cwd(), 'laya-owner-routing-home-'));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-owner-routing-checks.py'], {
      encoding: 'utf8', timeout: 60000,
      env: {...process.env, HOME: home, QMD_RECALL_LOG: '', QMD_SETUP_GUARD_FIXTURE: '',
            QMD_SKIP_BACKGROUND_WORKER: '1', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 21 tests/);
  } finally {
    removeTemp(home);
  }
});
