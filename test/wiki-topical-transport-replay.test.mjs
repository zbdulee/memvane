import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('scoped completed verification receipt replays after attestation crash', () => {
  const raw = execFileSync('python3', ['test/fixtures/wiki-topical-transport-replay.py'], {
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8', timeout: 30000,
  });
  const report = JSON.parse(raw);
  assert.equal(report.completedReceiptReplayed, true);
  assert.equal(report.tamperedTransportRejected, true);
  assert.equal(report.generationRecovered, true);
  assert.equal(report.retryReceiptRejected, true);
  assert.equal(report.scopedContextBounded, true);
  assert.equal(report.providerCalls, 2);
});
