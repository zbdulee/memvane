// Selected recall through hooks/run-hook and the query skill wrapper
// (tests/e2e/recall_e2e.py). Each method runs in its own python process with a
// fresh fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'recall_e2e.py');
const METHODS = [
  'test_linked_worktree_reads_its_own_view_and_a_branch_switch_is_cold',
  'test_recall_is_empty_after_the_last_card_is_retired',
  'test_query_skill_answers_from_the_selected_index',
];

for (const method of METHODS) {
  test(`recall e2e ${method}`, () => {
    const fixture = fixtureHome('mve');
    try {
      const r = spawnSync('python3', ['-B', SCRIPT, `RecallEndToEnd.${method}`], {
        cwd: ROOT, encoding: 'utf8', timeout: 300000, env: fixtureEnv(fixture, { QMD_SANDBOX: '1' }),
      });
      if (r.stdout) process.stdout.write(r.stdout);
      assert.ifError(r.error);
      assert.equal(r.signal, null, r.stderr);
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stderr, /Ran 1 test\b/);
      assert.match(r.stderr, /\nOK\b/);
    } finally {
      removeTemp(fixture.home);
    }
  });
}
