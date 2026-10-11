import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('fresh holdout regression retains incumbent and fences reviewed families', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-quality-retention-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 15000,
    });
  assert.deepEqual(JSON.parse(raw), {
    aggregate: 'retained', project: 'retained', improved: 'complete',
  });
});
