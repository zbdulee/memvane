import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// U17: end-to-end PR6-7 scenarios, one fixture install per scenario.
const GROUPS = {
  'full path, writes between steps, rollback round trip': [
    'test_01_full_path_from_v6_to_schema3_answers_the_v6_recall',
    'test_02_writes_between_steps_converge_and_recall_is_latest',
    'test_06_rollback_resume_and_switch_again',
  ],
  'SIGKILL at each boundary and parallel recalls during the CAS': [
    'test_03_sigkill_at_each_boundary_resumes_without_loss_or_duplicates',
    'test_04_parallel_recalls_during_the_cas_complete_in_old_or_new',
  ],
  'upgrade crossing, linked worktree and the other sqlite build': [
    'test_05_upgrade_finished_inside_the_migration_still_switches',
    'test_05b_stopped_upgrade_fence_restore_releases_recall_but_rollback_stays_deferred',
    'test_07_linked_worktree_reads_its_own_view_and_a_branch_switch_is_cold',
    'test_08_other_interpreter_advances_and_reads_the_same_stores',
  ],
};

for (const [title, methods] of Object.entries(GROUPS)) {
  const only = (process.env.MEMVANE_TEST_METHOD || '').split(',').filter(Boolean);
  const selected = only.length ? methods.filter((name) => only.includes(name)) : methods;
  test(`U17 ${title}`, { skip: selected.length === 0 }, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-pr67-')));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const result = spawnSync('python3', ['-B', 'test/fixtures/laya-pr67-integration-checks.py',
        ...selected.map((name) => `Pr67IntegrationChecks.${name}`)], {
        encoding: 'utf8', timeout: 1500000,
        env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
          MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1',
          QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
      });
      if (result.stdout) process.stdout.write(result.stdout);
      assert.ifError(result.error);
      assert.equal(result.status, 0, result.status === null ? JSON.stringify({ signal: result.signal })
        : result.stdout + result.stderr);
      assert.match(result.stderr, new RegExp(`Ran ${selected.length} tests?\\b`));
      assert.match(result.stderr, /\nOK(?: \(skipped=\d+\))?\n/);
    } finally {
      removeTemp(home);
    }
  });
}
