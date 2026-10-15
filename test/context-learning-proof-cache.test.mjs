import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('both host predictions reuse fresh proof or fall back without training', () => {
  const raw = execFileSync('python3', ['test/fixtures/context-learning-proof-cache-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
    encoding: 'utf8', timeout: 30000,
  });
  const result = JSON.parse(raw);
  for (const host of ['claude', 'codex']) {
    const report = result.hosts[host];
    for (const key of ['freshReuseWithoutSmoke', 'ageInvalidates', 'missingDenied',
      'modelChangeInvalidates', 'adapterChangeInvalidates', 'runtimeChangeInvalidates',
      'unsafeProofDenied', 'permissionDenied', 'malformedDenied', 'checksPreserved',
      'cacheRevocationHonored', 'memoryProofExpires', 'explicitTrainThenPredict',
      'stateUnchanged', 'noPredictionAttestation', 'expiryDuringProbeDenied',
      'revocationDuringProbeDenied', 'inferenceOnlyAfterTrainingExpiry',
      'inferenceOnlyWarmNoMetadata', 'inferenceOnlyMutationDenied', 'inferenceOnlyBindingRevocation']) assert.equal(report[key], true, `${host}:${key}`);
    assert.equal(report.actualModelCalls, 0);
    assert.equal(report.teacherCalls, 0);
  }
});
