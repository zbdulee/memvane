import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('raw recall quotes only the guard-verified revision and both routes list selected paths', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/recall-raw-excerpt-checks.py'],
    { encoding: 'utf8', timeout: 120000 }));
  assert.deepEqual(result, {
    changedAbsent: true, deletedAbsent: true, deniedAbsent: true, excludedAbsent: true,
    hostileLinesFramed: true, layaSelectionLogged: true, lexGatedStripped: true,
    matchPositionedExcerpt: true,
    optOutAbsent: true, presentSelectedPathTokenQuoted: true, quoteRecheckFailsClosed: true,
    unguardedNotQuoted: true,
  });
});
