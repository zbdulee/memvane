import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('historical trainer path resolves only to reviewed edit inference adapter', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-inference-argv-checks.py'], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.missingOldCacheResolved, true);
  assert.equal(result.currentAdapterHashAndViewPinned, true);
  assert.equal(result.invalidAdaptersRejectedBeforeExecution, true);
  assert.equal(result.trainingNotResolved, true);
});
