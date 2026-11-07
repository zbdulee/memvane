import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('U15 switch of a paired sidecar migration head keeps recall served', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-shadow-switch-sidecar-')));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, { mode: 0o700 });
  try {
    const selected=(process.env.MEMVANE_TEST_METHOD || '').split(',').filter(Boolean);
    const result = spawnSync('python3', ['-B', 'test/fixtures/laya-shadow-switch-sidecar-checks.py',
      ...selected.map(name=>`ShadowSwitchSidecarChecks.${name}`)], {
      encoding: 'utf8', timeout: 900000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.status === null ? JSON.stringify({ signal: result.signal })
      : result.stdout + result.stderr);
    assert.match(result.stderr, new RegExp(`Ran ${selected.length || 2} tests?\\b`));
    assert.match(result.stderr, /\nOK\n/);
  } finally {
    removeTemp(home);
  }
});
