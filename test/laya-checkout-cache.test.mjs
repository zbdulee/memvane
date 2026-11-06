import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('checkout fast path revalidates fresh metadata before reusing a Git proof', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'checkout-cache-home-')));
  try {
    const selected = process.env.MEMVANE_TEST_METHOD;
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-checkout-cache-checks.py', ...(selected ? [selected] : [])], {
      encoding: 'utf8', timeout: 120000, env: {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /\nOK\b/);
  } finally {
    removeTemp(home);
  }
});
