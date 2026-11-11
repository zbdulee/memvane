import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

for (const method of ['test_selected_codex_allow_is_empty',
                      'test_bundle_drift_codex_allow_is_empty',
                      'test_selected_claude_allow_is_empty',
                      'test_bundle_drift_claude_allow_is_empty',
                      'test_selected_and_drift_proof_change_rejected',
                      'test_worktree_codex_allow_is_empty',
                      'test_worktree_claude_allow_is_empty',
                      'test_worktree_other_hosts_and_proof_rejection',
                      'test_invalid_gate_exits_three_without_output_for_both_hosts',
                      'test_other_hosts_and_invalid_events_keep_their_behavior']) {
  test(`gate response: ${method}`, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'codex-gate-home-')));
    try {
      const child = spawnSync('python3', ['-B', 'test/fixtures/codex-gate-response-checks.py',
                                          `GateResponseChecks.${method}`], {
        encoding: 'utf8', timeout: 30000,
        env: {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
      });
      console.log(child.stdout.trim());
      assert.equal(child.status, 0, child.stdout + child.stderr);
    } finally {
      rmSync(home, {recursive: true, force: true});
    }
  });
}
