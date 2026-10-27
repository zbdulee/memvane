import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

test('compile and verify wiki writers share the wiki mutation lock without self-deadlock', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/wiki-mutation-lock-checks.py'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 15000,
      env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'},
    }));
  assert.deepEqual(result, {compileBlocked: true, verifyBlocked: true,
    nestedLockReentrant: true});
});
