import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('recall stage timing is body-free', { timeout: 300000 }, () => {
  const fixture = fixtureHome('mvr');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_recall_event_records_stage_times_without_prompt_or_body',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_unsealed_recall_scores_only_current_members_with_matching_sets',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_partial_recall_skips_a_mismatched_chunk_digest_and_keeps_lexical',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_a_cold_provider_serves_lexical_rows_and_wakes_the_owner_once',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_the_learning_selector_receives_the_published_rows'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || ''); assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
