import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('previous cache enrollment and transient candidate startup failures', {timeout: 60000}, () => {
  const fixture = fixtureHome('mv111review');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.learning.tests.integration.test_enrolled_warm',
      'memvane.learning.tests.integration.test_context_learning_candidate_apply',
      'memvane.learning.tests.integration.test_context_learning_selector_heal_rebind',
      'memvane.learning.tests.unit.test_context_learning_local_adapter_binding.LocalBinding.test_both_local_host_directions',
      'memvane.learning.tests.unit.test_context_learning_local_adapter_binding.LocalBinding.test_other_module_tampering_denied'],
    {encoding: 'utf8', timeout: 55000, env: fixtureEnv(fixture)});
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
