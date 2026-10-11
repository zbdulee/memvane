import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('queued SessionStart awaits QMD update before acknowledging a coalesced job', () => {
  const output = execFileSync('python3',
    ['test/fixtures/update-hook-product-await-checks.py'],
    { encoding: 'utf8' });
  assert.match(output, /awaited-update-ok/);
});
