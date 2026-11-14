// Onboarding -> recall with the fixture provider (model-free stand-in for the real-model run).
// Each method runs in its own python process with a fresh temporary HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from '../../test/helpers/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'onboarding_fixture.py');
const METHODS = [
  'test_project_without_cards_onboards_and_recall_adds_no_card',
  'test_project_with_thirty_cards_recalls_the_matching_card',
  'test_hot_store_journal_rolls_back_at_classification',
  'test_abandon_releases_unselected_installs_for_a_new_prepare',
  'test_source_change_between_finalize_and_activate_reseals',
  'test_rescan_after_selection_is_refused',
  'test_provider_change_releases_a_written_selection',
  'test_provider_change_release_reports_released',
  'test_reruns_compare_recorded_nodes_by_inode',
  'test_recall_in_interrupted_windows_exits_zero_without_context',
  'test_prepare_refuses_a_raw_tree_over_the_scan_bounds',
  'test_foreign_route_is_replaced_by_a_new_onboarding',
  'test_pointer_of_another_major_answers_without_hook_work',
  'test_interrupted_record_creation_reruns',
  'test_finalize_takes_the_sync_dispatch_check_as_a_port',
  'test_recover_without_ledger_never_abandons',
];

for (const method of METHODS) {
  test(`fixture onboarding ${method}`, () => {
    // The per-install socket path is at most 100 bytes: a short HOME directly
    // under /tmp keeps it within that bound (and inside a platform temp root).
    const home = realpathSync(mkdtempSync('/tmp/mvf-'));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const r = spawnSync('python3', ['-B', SCRIPT, `FixtureOnboarding.${method}`], {
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
