import test from 'node:test';
import {spawnSync} from 'node:child_process';

test('Codex and Claude SessionStart queue story-scoped work on one plugin DB', () => {
  const run = spawnSync('python3', ['test/fixtures/plugin-shared-sessionstart-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8',
    env: {...process.env, QMD_SETUP_GUARD_FIXTURE: '', QMD_RECALL_LOG: ''},
    timeout: 30000,
  });
  if (run.status !== 0) throw new Error(run.stdout + run.stderr);
});
