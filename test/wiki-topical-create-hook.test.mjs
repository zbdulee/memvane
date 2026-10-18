import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import { execFileSync } from 'node:child_process';

test('new source travels through detached hook, fake approved teacher, verification, and isolated QMD', (t) => {
  const result = JSON.parse(execFileSync('python3', ['test/fixtures/wiki-topical-create-hook-e2e.py'],
    { encoding: 'utf8', timeout: 90000 }));
  if (result.skipped) return t.skip(result.skipped);
  assert.ok(result.hookCreate && result.qmdIndexedEmbedded &&
    result.externalTeacherCalls === 0);
});
