// Wiki pipeline end to end: 3 host hooks -> edit inbox -> compile worker -> ledger -> published card,
// a publish killed mid-way, a backlog beyond 1,024 rows, a 1.x project from a release writer and a
// release bundle that writes nothing into a v2 project.
// Each method runs in its own python process with a fresh fixture HOME (tests/support/temp.mjs).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from '../support/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'wiki_pipeline_e2e.py');
const METHODS = [
  'test_three_hosts_feed_one_inbox_and_one_pass_publishes_one_verified_card',
  'test_a_worker_killed_inside_the_publish_resumes_without_paying_again',
  'test_a_backlog_beyond_1024_open_rows_does_not_stall_a_new_edit',
  'test_a_1x_project_written_by_a_release_writer_migrates_on_the_first_pass',
  'test_a_release_bundle_writes_nothing_into_a_v2_selected_project',
];

for (const method of METHODS) {
  test(`wiki pipeline e2e ${method}`, () => {
    // The per-install socket path is at most 100 bytes: a short HOME directly under /tmp.
    const fixture = fixtureHome('mve');
    try {
      const r = spawnSync('python3', ['-B', SCRIPT, `WikiPipelineE2E.${method}`], {
        cwd: ROOT, encoding: 'utf8', timeout: 300000, env: fixtureEnv(fixture),
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
