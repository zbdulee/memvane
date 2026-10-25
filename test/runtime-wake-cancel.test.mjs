import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('owned QA build wake cancel is revision-scoped, backed up, audited and fail-closed', () => {
  const result = JSON.parse(execFileSync('python3', ['test/fixtures/runtime-wake-cancel-checks.py'],
    { encoding: 'utf8', timeout: 120000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } }));
  assert.deepEqual(result, { appliedAudited: true, busyRefused: true, cliOfficialQueueOnly: true,
    failClosedNoMutation: true, fullRowAndLockedProof: true, raceKeepsNewer: true, recoveryNewRevision: true });
});
