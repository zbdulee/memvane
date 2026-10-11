import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('isolated multilingual training needs exact owner approval and writes only its output', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-isolated-train-checks.py'], {
      encoding: 'utf8', timeout: 15000,
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1',
        QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.validated, true);
  assert.equal(result.rejectedMutations, 10);
  assert.equal(result.outputOnly, true);
});
