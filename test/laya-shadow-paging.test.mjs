import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('one shadow step reads one effective inventory page at 3,749 cards', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-paging-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const reads = result.stdout.match(/paging_step_reads [^\n]+/);
  assert.ok(reads, 'per-step read observation missing');
  console.log(reads[0]);
  const oracle = result.stdout.match(/paging_merge_oracle_cases (\d+)/);
  assert.ok(oracle, 'merge oracle observation missing');
  assert.ok(Number(oracle[1]) > 0);
  assert.match(result.stderr, /Ran 2 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
