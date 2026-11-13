import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// Linked worktree view isolation on a fixture install.
const GROUPS = {
  'linked worktree reads its own view': [
    'test_07_linked_worktree_reads_its_own_view_and_a_branch_switch_is_cold',
  ],
};

for (const [title, methods] of Object.entries(GROUPS)) {
  const only = (process.env.MEMVANE_TEST_METHOD || '').split(',').filter(Boolean);
  const selected = only.length ? methods.filter((name) => only.includes(name)) : methods;
  test(`U17 ${title}`, { skip: selected.length === 0 }, () => {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-pr67-')));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const result = spawnSync('python3', ['-B', 'test/fixtures/laya-pr67-integration-checks.py',
        ...selected.map((name) => `Pr67IntegrationChecks.${name}`)], {
        encoding: 'utf8', timeout: 1500000,
        env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
          MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1',
          QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
      });
      if (result.stdout) process.stdout.write(result.stdout);
      assert.ifError(result.error);
      assert.equal(result.status, 0, result.status === null ? JSON.stringify({ signal: result.signal })
        : result.stdout + result.stderr);
      assert.match(result.stderr, new RegExp(`Ran ${selected.length} tests?\\b`));
      assert.match(result.stderr, /\nOK(?: \(skipped=\d+\))?\n/);
    } finally {
      removeTemp(home);
    }
  });
}
