import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('official selector adapters survive host/version paths without weakening identity', () => {
  const report = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-adapter-binding-checks.py'], {
      encoding: 'utf8', timeout: 30000,
      env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    }));
  for (const key of ['bothHosts', 'versionPaths', 'wrongAdapterDenied',
    'inventoryRequired', 'originRequired', 'manifestRequired', 'unsafePathsDenied',
    'policyAndCheckpointUnchanged', 'realTrainerConstructor', 'exactPathCompatible', 'deadlineBounded', 'claudeScopesUnambiguous']) {
    assert.equal(report[key], true, key);
  }
  assert.equal(report.modelCalls, 0);
  assert.equal(report.providerCalls, 0);
});
