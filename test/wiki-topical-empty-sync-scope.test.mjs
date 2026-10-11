import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

test('empty topical sync ignores legacy wiki while rejecting active retired topical cards', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/wiki-topical-empty-sync-scope.py'], { encoding: 'utf8' }));
  assert.deepEqual(result, { legacy_coexists: true, stale_topical_rejected: true });
});
