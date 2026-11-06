import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('migration captures proved selected wiki membership', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-migration-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const count = result.stdout.match(/synthetic_import_git_calls (\d+)/);
  assert.ok(count, 'Git call observation missing');
  console.log(`synthetic import Git calls: ${count[1]}`);
  const perf = result.stdout.match(/synthetic_hot_import_p95_ms ([0-9.]+) git_calls (\d+)/);
  assert.ok(perf, 'hot import timing observation missing');
  console.log(`synthetic hot import p95 ms: ${perf[1]}, Git calls: ${perf[2]}`);
  assert.match(result.stderr, /\nOK\b/);
});
