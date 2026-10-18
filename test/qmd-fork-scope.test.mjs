import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('source-only QMD scope prototype uses pre-limit FTS and pinned fake backend', () => {
  const result = spawnSync('python3', ['test/fixtures/qmd-fork-scope-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''}
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 4 tests/);
});
