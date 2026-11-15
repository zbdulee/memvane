// PR8 C10: compile -> verify -> compile intent -> sync pass -> compiled attestation.
// Each method runs in its own python process with a fresh temporary HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'wiki_db_provenance.py');
const METHODS = [
  'test_verified_intent_is_consumed_once_without_member_change',
  'test_intent_without_stamp_waits_without_store_write',
  'test_unacknowledged_consumption_replays_idempotently',
  'test_rejections_keep_other_intents_consumable_in_the_same_pass',
  'test_recall_is_identical_before_and_after_consumption',
];

for (const method of METHODS) {
  test(`wiki db provenance ${method}`, () => {
    // The per-install socket path is at most 100 bytes: a short HOME directly
    // under /tmp keeps it within that bound (and inside a platform temp root).
    const home = realpathSync(mkdtempSync('/tmp/mvp-'));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const r = spawnSync('python3', ['-B', SCRIPT, `WikiDbProvenance.${method}`], {
        cwd: ROOT, encoding: 'utf8', timeout: 300000,
        env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
          MEMVANE_TEST_RUNTIME_ROOT: runtime, PYTHONDONTWRITEBYTECODE: '1',
          QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
          GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0' },
      });
      if (r.stdout) process.stdout.write(r.stdout);
      assert.ifError(r.error);
      assert.equal(r.signal, null, r.stderr);
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stderr, /Ran 1 test\b/);
      assert.match(r.stderr, /\nOK\b/);
    } finally {
      removeTemp(home);
    }
  });
}
