import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('row-format scale observation', async () => {
  // The interpreter on PATH, like the other fixture tests (CI and both local Pythons).
  const python = 'python3';
  const home = await realpath(await mkdtemp(join(await realpath(tmpdir()), 's-scale-')));
  try {
    const child = spawn(python, ['-B', 'test/fixtures/laya-s-scale-checks.py'], {
      env: { ...process.env, HOME: home, TMPDIR: home, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SANDBOX: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '',
        GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout.pipe(process.stdout); child.stderr.pipe(process.stderr);
    const result = await new Promise((resolve, reject) => {
      child.once('error', reject); child.once('close', (code, signal) => resolve({ code, signal }));
    });
    assert.equal(result.signal, null, JSON.stringify(result));
    assert.equal(result.code, 0, JSON.stringify(result));
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});
