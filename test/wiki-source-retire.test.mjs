import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('selected source retirement archives only single-source machine cards', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/wiki-source-retire-checks.py'],
    { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Ran 11 tests/);
});
