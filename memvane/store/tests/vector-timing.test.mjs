import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('exact vector budget and stage timing', { timeout: 300000 }, () => {
  const fixture = fixtureHome('mvv');
  try {
    const prefix = 'memvane.store.tests.integration.test_view_vector_budget.VectorBudgetChecks.';
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      prefix + 'test_exact_counts_shared_content_once_and_reports_stage_timings',
      prefix + 'test_exact_scores_preferred_paths_first_without_changing_results',
      prefix + 'test_02_filtered_chunk_dimension_budget_preserves_lexical_on_defer',
      'memvane.store.tests.integration.test_view_vectors.VectorChecks.test_02_full_coverage_pins_members_and_deterministic_cosine_order',
      'memvane.store.tests.integration.test_view_vectors.VectorChecks.test_06_vector_rows_are_immutable_and_damaged_rows_refuse_the_pinned_lanes',
      'memvane.store.tests.integration.test_view_vectors.VectorChecks.test_11_work_counts_every_chunk_times_dimension_with_exact_boundary',
      'memvane.store.tests.integration.test_view_review_regressions.ReaderReviewChecks.test_exact_preserves_actual_vector_corruption_after_deadline',
      'memvane.store.tests.integration.test_view_review_regressions.ReaderReviewChecks.test_hybrid_does_not_hide_post_lexical_corruption_as_deferred'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture) });
    process.stdout.write(r.stdout || ''); process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
