import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('omitted selector root reuses private user state without changing project scope', () => {
  const report = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-default-state-root-checks.py'], {
      encoding: 'utf8', timeout: 30000,
      env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    }));
  for (const key of ['existingStateReused', 'differentHomes', 'spaces',
    'projectIsolation', 'descendantScope', 'explicitRootCompatible',
    'invalidExplicitDenied', 'unsafeRootsDenied', 'missingPreparationUnchanged']) {
    assert.equal(report[key], true);
  }
  assert.equal(report.externalCalls, 0);
});
