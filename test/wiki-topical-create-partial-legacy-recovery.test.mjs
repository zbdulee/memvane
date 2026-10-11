import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('topical create: legacy review and partially published siblings resume without provider repeats', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/wiki-topical-create-partial-legacy-recovery.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: ''}});
  const result = JSON.parse(raw);
  assert.deepEqual(result, {
    legacyReviewRequired: true,
    staleHashRejected: true,
    stableSiblingPair: true,
    partialPublishRecovered: true,
    providerCallsNotRepeated: true,
    reviewProofRevocation: true,
  });
});
