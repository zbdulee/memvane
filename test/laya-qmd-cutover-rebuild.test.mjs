import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('QMD drift after a bound cutover: retire, fresh install, crash resume and late-write fences', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-qmd-cutover-rebuild-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 17 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
