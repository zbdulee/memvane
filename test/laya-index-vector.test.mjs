import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Laya Index v6 title-bound exact vector contract uses disposable SQLite', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-index-vector-checks.py'], {
    encoding: 'utf8', timeout: 60000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 15 tests/);
  assert.doesNotMatch(result.stderr, /skipped=/);
});
