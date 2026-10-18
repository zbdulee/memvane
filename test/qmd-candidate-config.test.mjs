import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('candidate QMD index.yml policy is pure, narrow and fail-closed', () => {
  const p = spawnSync('python3', ['test/fixtures/qmd-candidate-config-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''}
  });
  assert.equal(p.status, 0, p.stdout + p.stderr);
  assert.match(p.stderr, /Ran 9 tests/);
  assert.match(p.stderr, /\nOK\b/);
});
