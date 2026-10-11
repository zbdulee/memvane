import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('synthetic root only runs hooks for the generated story', () => {
  const output = execFileSync('python3', ['test/fixtures/synthetic-invocation-scope-checks.py'],
    {cwd: process.cwd(), encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: ''}});
  assert.match(output, /synthetic-invocation-scope-ok/);
});
