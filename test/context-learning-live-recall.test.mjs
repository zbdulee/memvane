import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('opt-in live recall uses fifteen complete synthetic cards and preserves fallback', () => {
  const raw = execFileSync('python3', ['test/fixtures/context-learning-live-recall-e2e.py'], {
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8', timeout: 30000,
  });
  const report = JSON.parse(raw);
  for (const key of ['liveTop15', 'relevanceOrder', 'semanticZero',
    'timeoutFallback', 'unsupportedRuntimeFallback', 'captureMatchesLivePool',
    'staleIndexFallback', 'sharedWithoutGrantKeepsTopN',
    'adaptiveWiderCapture', 'overReturnNeverComplete']) assert.equal(report[key], true);
  assert.equal(report.externalCalls, 0);
});
