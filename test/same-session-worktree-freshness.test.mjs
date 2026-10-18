import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('recall drops same-session stale, deleted, out-of-scope and other-worktree hits per result', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/same-session-worktree-freshness-checks.py'],
    { encoding: 'utf8', timeout: 120000 }));
  assert.deepEqual(result, {
    fresh: true, modifiedDropped: true, sameContentReturn: true,
    deletedDropped: true, untrackedHandled: true, outOfScopeDropped: true,
    worktreeSwitchIsolated: true, unavailableFailsClosed: true,
    unguardedFixtureLabelled: true, boundedAccounting: true,
  });
});
