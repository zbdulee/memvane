import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('new project with an empty wiki: build seals, activates and recalls nothing', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-fresh-onboarding-empty-wiki-checks.py'], {
    encoding: 'utf8', timeout: 300000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 2 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
