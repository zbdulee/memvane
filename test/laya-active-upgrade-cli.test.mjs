import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';

test('selected bundle CLI requires explicit action approval before a write', () => {
  const root = mkdtempSync(join(tmpdir(), 'laya-active-cli-'));
  try {
    const project = join(root, 'project');
    const state = join(root, 'state');
    mkdirSync(project);
    mkdirSync(state, {mode: 0o700});
    for (const action of ['upgrade-active-index-bundle', 'restore-active-index-bundle']) {
      const run = spawnSync('python3', ['-B', 'core/setup_entry.py', action,
        '--project', project, '--state-root', state], {
        encoding: 'utf8', timeout: 15000,
        env: {...process.env, QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
          PYTHONDONTWRITEBYTECODE: '1'},
      });
      assert.equal(run.status, 1, run.stderr + run.stdout);
      const result = JSON.parse(run.stdout);
      assert.equal(result.status, 'rejected');
      assert.equal(result.reason, 'active_index_upgrade_approval_required');
      assert.equal(result.changesApplied, 0);
    }
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
});
