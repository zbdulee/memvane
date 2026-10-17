import test from 'node:test';
import {spawnSync} from 'node:child_process';

test('SessionStart detects pull and branch file changes without enabling compile', () => {
  const run = spawnSync('python3', ['test/fixtures/sessionstart-sync-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  if (run.status !== 0) throw new Error(run.stdout + run.stderr);
});

test('previous version-1 sync snapshots remain readable', () => {
  const run = spawnSync('python3', ['test/fixtures/sync-v1-compat-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  if (run.status !== 0) throw new Error(run.stdout + run.stderr);
});
