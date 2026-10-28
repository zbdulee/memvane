import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('v45 approved trainer reference remains readable but cannot execute after upgrade', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-v45-compat-checks.py'], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.missingOldCacheAcceptedForRead, true);
  assert.equal(result.tamperedOldCacheRejected, true);
  assert.equal(result.trainingAndObservationBlocked, true);
});
