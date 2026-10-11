import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('gold-only worker keeps provenance gates and skips every checkpoint action', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-gold-only-checks.py'], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.provenanceChecked, true);
  assert.equal(result.teacherCalled, true);
  assert.equal(result.checkpointSkipped, true);
  assert.equal(result.manualGoldGuard, true);
});
