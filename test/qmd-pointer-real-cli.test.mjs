import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

test('local QMD 2.5.3 typed query returns JSON list from an isolated synthetic index', (t) => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/qmd-pointer-real-cli.py'], { encoding: 'utf8', timeout: 240000 }));
  if (result.skipped) return t.skip(result.skipped);
  assert.equal(result.typedJsonList, true);
  assert.ok(result.isolatedIndexBytes > 0);
  assert.equal(result.externalCalls, 0);
});
