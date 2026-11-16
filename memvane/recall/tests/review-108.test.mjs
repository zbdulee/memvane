import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';
test('semantic deferral retains lexical selection', () => {
  const fixture = fixtureHome('mvr');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.recall.tests.integration.test_schema3_recall.Schema3Recall.test_large_semantic_deadline_preserves_lexical_recall'],
      { env: fixtureEnv(fixture), encoding: 'utf8', timeout: 180000 });
    process.stderr.write(result.stderr || '');
    assert.ifError(result.error); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});
