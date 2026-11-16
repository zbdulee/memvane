import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('capture source diagnostics preserve the learning request schema', { timeout: 180000 }, () => {
  const fixture = fixtureHome('mvp');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_semantic_set_validation_errors_keep_lexical_selector_and_reason',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_actual_input_sha_mismatch_preserves_lexical_in_both_vector_lanes',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_semantic_sources_and_kernel_statistics_reach_all_host_events',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_semantic_store_deadlines_keep_lexical_candidates_and_selector',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_nonfinite_semantic_rows_keep_lexical_recall'],
    { encoding: 'utf8', timeout: 170000, env: fixtureEnv(fixture) });
    process.stdout.write(r.stdout || ''); process.stderr.write(r.stderr || '');
    assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
