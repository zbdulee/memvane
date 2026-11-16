import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fixtureEnv, fixtureHome, removeTemp } from '../../../tests/support/temp.mjs';

test('log summary reports recall timing', { timeout: 300000 }, () => {
  const fixture = fixtureHome('mvd');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', '-v',
      'memvane.diagnostics.tests.integration.test_diagnostic_records.DiagnosticRecordTests.test_logs_summary_reports_latency_semantic_and_stage_medians'],
    { cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture) });
    process.stderr.write(r.stderr || ''); assert.ifError(r.error); assert.equal(r.signal, null); assert.equal(r.status, 0);
  } finally { removeTemp(fixture.home); }
});
