import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('Codex sparse marketplace upgrades and unavailable cache diagnostics', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-codex-sparse-upgrade-checks.py'], {
    encoding: 'utf8', timeout: 120000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /Ran 9 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
