import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('raw recall selects verified source locators without injecting original text', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/recall-raw-excerpt-checks.py'],
    { encoding: 'utf8', timeout: 120000 }));
  assert.deepEqual(result, {
    changedSourceDropped: true, deletedSourceDropped: true, deniedReadNotServed: true,
    excludedSourceDropped: true, fullHostProbeQuestionSpanWithoutRawBody: true,
    layaSelectionLogged: true, lexGateNeverQuotesRaw: true, optOutNotServed: true,
    unverifiedMetadataNotInjected: true, verifiedLocatorWithoutBody: true,
  });
});
