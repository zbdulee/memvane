import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('isolated train CLI dispatches both operations and fails closed without a grant', () => {
  const home = mkdtempSync(join(process.cwd(), 'qmd-isolated-cli-'));
  try {
    const project = join(home, 'work', 'novel');
    mkdirSync(project, {recursive: true});
    const env = {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1',
      QMD_RECALL_LOG: ''};
    const invoke = (...args) => spawnSync('python3',
      ['core/context_learning_isolated.py', ...args],
      {cwd: process.cwd(), env, encoding: 'utf8', timeout: 5000});
    const usage = invoke();
    assert.equal(usage.status, 2, usage.stderr);
    assert.equal(usage.stdout, '');
    for (const action of ['preflight', 'run']) {
      const result = invoke(action, project);
      assert.equal(result.status, 1, result.stderr);
      const payload = JSON.parse(result.stdout);
      assert.equal(payload.status, 'failed');
      assert.ok(payload.reason);
      assert.equal(result.stderr, '');
    }
    assert.equal(existsSync(join(home, '.config/qmd/isolated-train-approval/current.json')), false);
    assert.equal(existsSync(join(home, '.local/share/qmd-auto-context/isolated-runs')), false);
  } finally {
    removeTemp(home);
  }
});
