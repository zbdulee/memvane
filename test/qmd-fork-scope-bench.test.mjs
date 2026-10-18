import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('model-free FTS benchmark records scope recall and timing', () => {
  const result = spawnSync('python3', ['test/fixtures/qmd-fork-scope-bench.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''}
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  const report = JSON.parse(result.stdout);
  assert.equal(report.synthetic, true);
  assert.equal(report.scoped_full_match.recall_at_5, 1);
  assert.equal(report.legacy_global.recall_at_5, 0);
  console.log(result.stdout.trim());
});
