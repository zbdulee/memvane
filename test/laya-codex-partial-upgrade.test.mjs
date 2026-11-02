import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

test('real Codex promisor clones prove installed bytes without lazy fetch', () => {
  const result = spawnSync('python3', ['-B', 'test/fixtures/laya-codex-partial-upgrade-checks.py'], {
    encoding: 'utf8', timeout: 600000,
    env: {...process.env, QMD_SANDBOX: '1', QMD_SETUP_GUARD_FIXTURE: '1',
      QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  // Preserve measured Git evidence on both RED and GREEN.
  if (result.stdout) process.stdout.write(result.stdout);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Ran 8 tests/);
  assert.match(result.stderr, /\nOK\b/);
});
