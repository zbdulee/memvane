import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
for (const name of ['GenerationSync', 'RowSync']) for (const method of ['test_unchanged_pass_consumes_intent_once', 'test_tampered_parity_is_rejected_without_store_write', 'test_intent_with_absent_card_waits_without_write', 'test_newer_consumed_supersedes_older_waiting', 'test_waiting_queue_rotates_to_valid_intent', 'test_malformed_intent_does_not_block_valid_intent', 'test_forged_consumed_marker_cannot_supersede_waiting', 'test_swapped_intent_is_never_acknowledged']) {
  test(`${name} ${method}`, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'pr8-c3-sync-')));
    try {
      const r = spawnSync('python3', ['-B', 'test/fixtures/pr8-c3-sync-red.py', `${name}.${method}`], {
        encoding: 'utf8', timeout: 180000, env: {...process.env, HOME: home, TMPDIR: home,
          PYTHONDONTWRITEBYTECODE: '1', QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
          GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0'},
      });
      if (r.stdout) process.stdout.write(r.stdout);
      if (r.stderr) process.stdout.write(r.stderr);
      assert.ifError(r.error); assert.equal(r.status, 0);
    } finally { rmSync(home, {recursive: true, force: true}); }
  });
}
