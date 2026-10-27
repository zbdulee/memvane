import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {removeTemp} from './helpers/temp.mjs';

const env = (home) => ({...process.env, HOME: home, QMD_RECALL_LOG: '',
  QMD_SKIP_BACKGROUND_WORKER: '1', PYTHONDONTWRITEBYTECODE: '1'});

test('minimal settings model, legacy invariance, routes and generation skips', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'minimal-settings-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/minimal-settings-checks.py'], {
      encoding: 'utf8', timeout: 120000, env: env(home),
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 12 tests/);
  } finally { removeTemp(home); }
});

test('--enable-compile leaves a minimal settings file byte-for-byte unchanged', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'minimal-settings-compile-')));
  try {
    const project = join(home, 'project');
    mkdirSync(join(project, '.auto-context'), {recursive: true});
    const settings = join(project, '.auto-context', 'settings.json');
    const bytes = JSON.stringify({name: 'project', skipPaths: ['build']}, null, 2) + '\n';
    writeFileSync(settings, bytes);
    const result = spawnSync('bash', ['core/update.sh', '--enable-compile', project], {
      encoding: 'utf8', timeout: 60000, env: env(home),
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stdout, /minimal settings/);
    assert.equal(readFileSync(settings, 'utf8'), bytes);
    assert.ok(readFileSync(join(project, '.auto-context', 'wiki', 'SCHEMA.md'), 'utf8'));
  } finally { removeTemp(home); }
});
