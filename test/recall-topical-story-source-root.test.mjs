import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

test('only scoped topical cards resolve story-relative revisions against the story', () => {
  const output = execFileSync('python3', ['test/fixtures/recall-topical-story-source-root.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  const result = JSON.parse(output);
  assert.deepEqual(result, {
    selectedStory: true, trusted: true, sourcePresent: 1,
    fresh: 'fresh', parentFresh: 'stale', legacyParent: true,
    foreignParent: true, noScopeParent: true,
    parentCollisionFresh: 'fresh', protected: true, deniedTrusted: false,
    deniedRevisionsRemoved: true, legacyUnprotected: true,
    roleDriftOutputEmpty: true,
  });
});
