import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('4000 trusted candidate members use bounded sample steps', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-parity-scale-checks.py'], {
    encoding: 'utf8', env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  process.stdout.write(result.stdout || '');
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
