import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('derived split admission pins assignments, families, exclusions, and payloads', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-derived-split-checks.py'], {
      encoding: 'utf8', timeout: 15000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.validated, true);
  assert.equal(result.rejectedMutations, 12);
  assert.equal(result.trainingCalls, 0);
});
