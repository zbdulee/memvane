import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('synthetic QA RSS ceiling is 3,584 MiB with the reviewed profile rule', () => {
  const result = JSON.parse(execFileSync('python3', ['test/fixtures/synthetic-rss-ceiling-checks.py'],
    { encoding: 'utf8', timeout: 60000 }));
  assert.deepEqual(result, {boundary2304: 'accepted', measured2033: 'accepted',
    otherBoundsUnchanged: true, over2305: 'setup_synthetic_measured_profile_required'});
});
