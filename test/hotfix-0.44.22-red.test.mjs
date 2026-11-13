import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const cases = [
  ['switch budget', 'laya-shadow-switch-checks.py', 'ShadowSwitchChecks.test_10_switch_proof_budget_is_thirty_seconds'],
  ['switch rehearsal', 'laya-shadow-switch-checks.py', 'ShadowSwitchChecks.test_11_rehearsal_reports_gates_without_any_persistent_write'],
  ['resident starvation', 'laya-resident-service-checks.py', 'ServiceChecks.test_drift_reproof_yields_to_loading_full_proof'],
  ['progress retry', 'runtime-wake-policy-checks.py', 'PolicyDecisions.test_sync_durable_progress_retries_without_counting_only_new_details'],
  ['ingest progress', 'laya-schema3-sync-publish-checks.py', 'Schema3SyncPublishChecks.test_ingest_pending_detail_requires_committed_progress'],
  ['Codex notice', 'laya-host-response-checks.py', 'ResponseChecks.test_codex_seal_is_announced_once_in_claude_session_start'],
  ['Codex notice during drift', 'laya-host-response-checks.py', 'BundleDriftChecks.test_claude_drift_session_includes_pending_codex_seal_notice'],
];
for (const [name, file, method] of cases) {
  test(name, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'hotfix-red-')));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const result = spawnSync('python3', ['-B', '-c',
        'import runpy, sys; print("child Python:", sys.version, flush=True); sys.argv.pop(0); runpy.run_path(sys.argv[0], run_name="__main__")',
        `test/fixtures/${file}`, method], {
        encoding: 'utf8', timeout: 300000,
        env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
          QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
      });
      if (result.stdout) process.stdout.write(result.stdout);
      if (result.stderr) process.stdout.write(result.stderr);
      assert.ifError(result.error);
      assert.equal(result.status, 0, result.signal || 'fixture failed');
    } finally { rmSync(home, { recursive: true, force: true }); }
  });
}
