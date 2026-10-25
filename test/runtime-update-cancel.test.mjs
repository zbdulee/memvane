import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('owned QA update wake cancel is revision-scoped, locked, backed up, audited and fail-closed', () => {
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' };
  delete env.QMD_UPDATE_HOOK_QUEUE_DIR;
  const result = JSON.parse(execFileSync('python3', ['test/fixtures/runtime-update-cancel-checks.py'],
    { encoding: 'utf8', timeout: 120000, env }));
  assert.deepEqual(result, { appliedAuditedOthersKept: true, backupFailureNoMutation: true,
    cliOfficialQueueOnly: true, driftAndRaceKeepRow: true, failClosedNoMutation: true,
    replayAuditedOnly: true, runningOrBusyRefused: true });
});
