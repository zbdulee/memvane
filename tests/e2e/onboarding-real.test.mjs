// Real-model onboarding -> recall through entry/setup.py and hooks/run-hook.
// Runs only with QMD_REAL_MODEL_TESTS=1 and MEMVANE_TEST_MODEL_FILE (the pinned
// GGUF) under `npm run dev:guard -- --kind model-test -- node --test <this>`.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from '../../test/helpers/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'onboarding_real.py');
const enabled = process.env.QMD_REAL_MODEL_TESTS === '1' && process.env.MEMVANE_TEST_MODEL_FILE;
const HOME_PREFIX = '/private/tmp/mvo-';

// The resident service and its runtime monitor detach (new session) and never
// idle-exit, so they outlive a run the guard stops. The python driver stops
// them on SIGTERM; this sweep covers a SIGKILLed run. It stops exactly the
// processes that run this checkout's entry or a runtime under a test HOME.
function stopDetached(home) {
  const listing = execFileSync('ps', ['-axww', '-o', 'pid=,command='], { encoding: 'utf8' });
  for (const line of listing.split('\n')) {
    const match = line.trim().match(/^(\d+)\s+(.*)$/);
    if (!match || Number(match[1]) === process.pid) continue;
    const command = match[2];
    const ours = command.includes(join(ROOT, 'entry', 'run.py')) || command.includes('laya_local_embed_worker.mjs');
    if (ours && command.includes(home ?? HOME_PREFIX)) {
      try { process.kill(Number(match[1]), 'SIGTERM'); } catch { /* already gone */ }
    }
  }
}

test('real-model onboarding serves recall from the process entry points', {
  skip: enabled ? false : 'needs QMD_REAL_MODEL_TESTS=1 and MEMVANE_TEST_MODEL_FILE',
}, () => {
  // A short HOME directly under /tmp keeps per-install socket paths short.
  stopDetached();  // leftovers of an earlier run the guard stopped
  const home = realpathSync(mkdtempSync('/tmp/mvo-'));
  const runtime = join(home, 'rt');
  mkdirSync(runtime, { mode: 0o700 });
  try {
    const env = { PATH: process.env.PATH, HOME: home, TMPDIR: home, USER: process.env.USER,
      LANG: 'C.UTF-8', MEMVANE_TEST_RUNTIME_ROOT: runtime, MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT: '1', PYTHONDONTWRITEBYTECODE: '1',
      MEMVANE_TEST_MODEL_FILE: process.env.MEMVANE_TEST_MODEL_FILE,
      GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0' };
    const r = spawnSync('python3', ['-B', SCRIPT], { cwd: ROOT, encoding: 'utf8', env,
      timeout: 1700000 });
    if (r.stdout) process.stdout.write(r.stdout);
    if (r.stderr) process.stderr.write(r.stderr.slice(-8000));
    assert.ifError(r.error);
    assert.equal(r.signal, null);
    assert.equal(r.status, 0);
    assert.match(r.stdout, /"stage": "done"/);
  } finally {
    stopDetached(home);
    removeTemp(home);
  }
});
