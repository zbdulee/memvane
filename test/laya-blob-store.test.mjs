import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('immutable blob candidate admits only bound wiki cards', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-blob-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-blob-store-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 18 tests/);
  } finally {
    removeTemp(home);
  }
});
