import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fixtureEnv, fixtureHome, removeTemp} from '../support/temp.mjs';

test('fresh checkout proof can seed the next capture', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvc');
  try {
    const result = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-v',
      '-s', 'memvane/store/tests/integration', '-t', '.', '-p', 'test_checkout_cache.py',
      '-k', 'fresh_proof_populates_cache'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture)});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
  } finally { removeTemp(fixture.home); }
});

// One prompt recall through hooks/run-hook spawns as many git children for
// store records sealed under another boot's device number as for current ones
// (tests/e2e/checkout_cache_e2e.py). darwin only: elsewhere the cache is off.
test('recall git children do not grow after the device number moved', {timeout: 300000}, () => {
  const fixture = fixtureHome('mvc');
  try {
    const result = spawnSync('python3', ['-B', 'tests/e2e/checkout_cache_e2e.py'],
      {cwd: process.cwd(), encoding: 'utf8', timeout: 290000, env: fixtureEnv(fixture, {QMD_SANDBOX: '1'})});
    process.stdout.write(result.stdout || ''); process.stderr.write(result.stderr || '');
    assert.equal(result.signal, null); assert.equal(result.status, 0);
    assert.match(result.stderr, /Ran 1 test\b/);
  } finally { removeTemp(fixture.home); }
});
