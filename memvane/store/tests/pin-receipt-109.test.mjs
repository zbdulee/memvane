import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('pin receipt verifies a generation across readers', () => {
  const fixture = fixtureHome('mvs');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.store.tests.integration.test_pin_receipt.PinReceipt.test_reopen_reuses_only_matching_private_receipt',
      'memvane.store.tests.integration.test_pin_receipt.PinReceipt.test_independent_process_reuses_unchanged_generation',
      'memvane.store.tests.integration.test_pin_receipt.PinReceipt.test_mutated_card_forces_full_verification_and_refusal'],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error);
    assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
