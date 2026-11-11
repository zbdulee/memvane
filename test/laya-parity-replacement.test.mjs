import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('trusted count and bounded candidate search replace old parity', () => {
  const args = ['-B', 'test/fixtures/laya-parity-replacement-checks.py'];
  if (process.env.PARITY_CASE) args.push(process.env.PARITY_CASE);
  const result = spawnSync('python3', args, {
    encoding: 'utf8', env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  process.stdout.write(result.stdout || '');
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /\nOK\b/);
});
