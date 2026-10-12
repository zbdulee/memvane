import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('bounded similarity review persists a successor and resumes once', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/topical-similarity-resume-queue.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: ''}});
  assert.deepEqual(JSON.parse(raw), {
    durableSuccessor: true, noHotLoop: true, settledAfterRestart: true,
  });
});
