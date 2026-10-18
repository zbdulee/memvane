import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Laya Index migration and rollback rehearsal uses synthetic temporary DBs', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-index-migration-checks.py'], {
    encoding: 'utf8', timeout: 45000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 28 tests/);
  assert.doesNotMatch(result.stderr, /skipped=/);
});
