import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdirSync, mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('synthetic candidate vector growth and filtered float work preserve pinned lexical results', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-view-vector-budget-home-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, {mode: 0o700});
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-view-vector-budget-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        QMD_CACHE_DIR: join(home, 'cache'), QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1'},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr + (result.error?.message ?? ''));
    assert.match(result.stderr, /Ran 2 tests in /);
    assert.match(result.stderr, /\nOK\s*$/);
    const records = result.stdout.trim().split('\n').map(line => JSON.parse(line));
    assert.deepEqual(records.map(row => row.case), ['synthetic_growth', 'synthetic_filtered_work']);
    for (const record of records) console.log(JSON.stringify(record));
  } finally {
    removeTemp(home);
  }
});
