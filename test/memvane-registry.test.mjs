import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

// Selected project registry v1: store, setup lifecycle, reconcile, SessionStart,
// external edit routing and the base release beside a registry.
const env = {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_SANDBOX: '1',
  QMD_SETUP_GUARD_FIXTURE: '1'};

function run(t, fixture, name) {
  const result = spawnSync('python3', ['-B', fixture, name],
    {encoding: 'utf8', timeout: 300000, env});
  // Record how many checks ran and were skipped, so a log tells both apart.
  for (const line of (result.stderr || '').split('\n')) {
    if (/^(Ran \d+ tests?|OK|FAILED)/.test(line) || /skipped/.test(line)) {
      t.diagnostic(line.trim());
    }
  }
  assert.equal(result.status, 0, result.stdout + result.stderr);
}

for (const name of ['Store', 'Lifecycle', 'Reconcile', 'SessionStart']) {
  test(`selected project registry: ${name}`, (t) => {
    run(t, 'test/fixtures/memvane-registry-checks.py', name);
  });
}

for (const name of ['RegistryTargets', 'MixedBundle']) {
  test(`selected project registry routing: ${name}`, (t) => {
    run(t, 'test/fixtures/memvane-registry-routing-checks.py', name);
  });
}
