import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('historical runtime rejects before real queued-case review sync', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-historical-sync-guard-checks.py'], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.queuedCases, 2);
  assert.equal(result.guardPreservedBytes, true);
  assert.equal(result.realSyncWouldMutate, 2);
  assert.equal(result.telemetryDeferred, true);
});
