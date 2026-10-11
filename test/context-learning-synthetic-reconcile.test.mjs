import {test} from 'node:test';
import {strict as assert} from 'node:assert';
import {execFileSync} from 'node:child_process';

test('synthetic SessionStart inventory archives stale cards and survives a crash', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-synthetic-reconcile-checks.py'], {
      encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      timeout: 15000,
    }));
  for (const key of ['deleteRetiresExactCard', 'otherSharedSourcePreserved',
    'multiSourceRestoreRequiresAllCurrent',
    'crashReplay', 'repeatedStartIdempotent', 'updatesAndCreatesNeedOrigin',
    'chainedRevisionReplayed', 'reviewOnlyCardDoesNotBlockGenerated',
    'revertedCurrentCardPreserved', 'exactArchiveRestoreAndRetire',
    'restoreQueueCrashThenReedit', 'abandonedRestoreTempCleaned',
    'conflictingRestoreDoesNotStrandRetirement']) {
    assert.equal(result[key], true);
  }
  assert.equal(result.externalCalls, 0);
});
