import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('opt-in live recall uses fifteen complete synthetic cards and preserves fallback', () => {
  const home = mkdtempSync(join(tmpdir(), 'qmd-live-recall-home-'));
  mkdirSync(join(home, 'work'));
  mkdirSync(join(home, 'locks'));
  try {
    const raw = execFileSync('python3', ['test/fixtures/context-learning-live-recall-e2e.py'], {
      env: {...process.env, HOME: home, CODEX_HOME: join(home, '.codex'), QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1',
        QMD_LOCK_BASE: join(home, 'locks'), QMD_SKIP_BACKGROUND_WORKER: '1'},
      encoding: 'utf8', timeout: 30000,
    });
    const report = JSON.parse(raw);
    for (const key of ['liveTop15', 'relevanceOrder', 'semanticZero',
      'timeoutFallback', 'unsupportedRuntimeFallback', 'captureMatchesLivePool',
      'staleIndexFallback', 'sharedWithoutGrantKeepsTopN',
      'adaptiveWiderCapture', 'overReturnNeverComplete',
      'acceptedProfileWithoutCaptureRoutesSharedActive']) assert.equal(report[key], true);
    assert.equal(report.externalCalls, 0);
  } finally { rmSync(home, {recursive: true, force: true}); }
});
