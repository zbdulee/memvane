import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('normal scheduler stages fresh cohort training without publishing a pointer', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-auto-staging-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        QMD_SKIP_BACKGROUND_WORKER: '1', PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 10000,
    });
  const result = JSON.parse(raw);
  assert.equal(result.staged_train, true);
  assert.equal(result.backlog_preserved, true);
  assert.equal(result.evaluation_no_pointer, true);
  assert.equal(result.revoke_blocks, true);
});
