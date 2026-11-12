import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';

// Selected project registry v1: store, setup lifecycle, reconcile, SessionStart.
const env = {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_SANDBOX: '1',
  QMD_SETUP_GUARD_FIXTURE: '1'};

for (const name of ['Store', 'Lifecycle', 'Reconcile', 'SessionStart']) {
  test(`selected project registry: ${name}`, () => {
    const result = spawnSync('python3', ['-B', 'test/fixtures/memvane-registry-checks.py', name],
      {encoding: 'utf8', timeout: 300000, env});
    assert.equal(result.status, 0, result.stdout + result.stderr);
  });
}

for (const name of ['RegistryTargets', 'MixedBundle']) {
  test(`selected project registry routing: ${name}`, () => {
    const result = spawnSync('python3',
      ['-B', 'test/fixtures/memvane-registry-routing-checks.py', name],
      {encoding: 'utf8', timeout: 300000, env});
    assert.equal(result.status, 0, result.stdout + result.stderr);
  });
}
