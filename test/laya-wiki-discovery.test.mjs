import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('selected v2 raw changes reach the wiki source queue only under an accepted grant', () => {
  const result = spawnSync('python3', ['test/fixtures/laya-wiki-discovery-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      QMD_SYNC_COMPILE_MAX: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 35 tests in/);
});
