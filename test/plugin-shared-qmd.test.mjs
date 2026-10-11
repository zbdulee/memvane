import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync, spawnSync} from 'node:child_process';

test('plugin QMD store and story scopes fail closed before candidate reads', () => {
  const run = spawnSync('python3', ['test/fixtures/plugin-shared-qmd-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8',
    env: {...process.env, QMD_SETUP_GUARD_FIXTURE: '', QMD_RECALL_LOG: ''},
    timeout: 30000,
  });
  if (run.status !== 0) throw new Error(run.stdout + run.stderr);
});

test('production QMD route blocks a global update after another story opts out', () => {
  const output = execFileSync('python3',
    ['test/fixtures/plugin-shared-enrollment-cli-checks.py'],
    { encoding: 'utf8' });
  assert.match(output, /shared-enrollment-cli-ok/);
});
