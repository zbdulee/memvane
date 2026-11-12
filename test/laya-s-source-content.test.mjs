import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('S source content identity is independent of checkout and rejects inconsistent rows', () => {
  const python = process.env.LANE_B_PYTHON;
  assert.ok(['/usr/bin/python3', '/opt/homebrew/bin/python3.13'].includes(python));
  const home = realpathSync(mkdtempSync(join(realpathSync(tmpdir()), 's-source-')));
  try {
    const result = spawnSync(python, ['-B', 'test/fixtures/laya-s-source-content-checks.py'], {
      env: { ...process.env, HOME: home, TMPDIR: home, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', GIT_CONFIG_GLOBAL: '/dev/null',
        GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0' }, encoding: 'utf8',
    });
    process.stdout.write(result.stdout || '');
    process.stderr.write(result.stderr || '');
    assert.equal(result.status, 0, `signal=${result.signal} error=${result.error}`);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
