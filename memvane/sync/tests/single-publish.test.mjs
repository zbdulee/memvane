import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('sealed vector head publishes once and deferral keeps lexical recall', { timeout: 300000 }, () => {
  const fixture = fixtureHome('mvs');
  try {
    const prefix = 'memvane.sync.tests.integration.test_schema3_vectors.SyncVectors.';
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      prefix + 'test_sealed_head_edit_publishes_one_vector_generation',
      prefix + 'test_resource_refusal_keeps_the_lexical_head_then_the_head_fill_seals_vectors',
      prefix + 'test_provider_defect_ends_the_wake_blocked_and_a_refusal_stays_deferred',
      prefix + 'test_ingest_main_view_receives_vector_window',
      prefix + 'test_a_cold_target_gets_the_resume_budget',
      prefix + 'test_progressing_pass_acks_and_reoffers_while_a_zero_progress_pass_keeps_its_wake',
      prefix + 'test_a_yield_after_progress_reoffers_and_the_next_pass_repeats_no_embedding'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture) });
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
