import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Laya vector development rehearsal remains separate from formal L1', (t) => {
  const result = spawnSync('python3', ['test/fixtures/laya-l1-qa-checks.py'], {
    encoding: 'utf8', timeout: 180000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /FORMAL_RELEASE_L1=NOT_RUN/);
  assert.match(result.stdout, /DEVELOPMENT_VECTOR_ONLY=RUN/);
  assert.match(result.stderr, /Ran 2 tests/);
  assert.doesNotMatch(result.stderr, /skipped=/);
  for (const label of ['BLOCKED_CLAUDE_RECALL', 'BLOCKED_CLAUDE_INDEX',
    'BLOCKED_CODEX_RECALL', 'BLOCKED_CODEX_INDEX',
    'DEVELOPMENT_VECTOR_SYNC_QUERY']) {
    const metric = result.stdout.match(new RegExp(`TEMP_${label}_MS=([0-9]+)`));
    assert.ok(metric, `missing ${label} latency: ${result.stdout}`);
    t.diagnostic(`TEMP_${label}_MS=${metric[1]}`);
  }
});
