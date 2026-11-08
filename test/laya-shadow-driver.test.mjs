import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('a completed selected sync pass advances the private shadow migration by bounded steps', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-driver-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  // Measurement only: per-pass driver steps and wall time after the sync.
  console.log(result.stdout.split('\n').filter((line) => /^(per_pass|pass_ms) |^\{"case": "shadow-hot-journal"/.test(line)).join('\n'));
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 11 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
