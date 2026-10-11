import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('topical worker lineage binds multisource snapshots and QMD ACK through retirement', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-topical-worker-provenance-checks.py'], {
      env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      encoding:'utf8',timeout:15000}));
  assert.deepEqual(result, {sourceAckRequired:true,oneRegistration:true,
    indexTamperDenied:true,missingProofDenied:true,
    sharedReferenceRetained:true,retirementExcluded:true});
});
