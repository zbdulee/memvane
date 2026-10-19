import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('verifier-cited source spans: derivation, closed frontmatter and atomic stamp', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/source-spans-checks.py'], {
    encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 18 tests/);
});
