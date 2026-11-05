import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdirSync, mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('selected worker prewarm reproof uses owned readers', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-prewarm-proof-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, {mode: 0o700});
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-sync-first-proof-checks.py',
      'SyncFirstProofChecks.test_prewarm_changed_route_refused_before_old_queue_owner',
      'SyncFirstProofChecks.test_prewarm_reproof_uses_owned_readers_before_provider_start'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
  } finally { removeTemp(home); }
});
