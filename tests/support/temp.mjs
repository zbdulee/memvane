// Temporary fixture directories for node tests (S5 L0 replacement of test/helpers/temp.mjs).
//
// A fixture HOME is a realpath'd, owner-private directory directly under /tmp:
// the per-install socket path under HOME is at most 100 bytes, and product
// test runtime roots must be inside a platform temp root. Nothing here reads
// or writes the account home.
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { join } from 'node:path';

const PAUSE = new Int32Array(new SharedArrayBuffer(4));

/** Remove a temporary tree; a late writer (ENOTEMPTY/EBUSY) is retried in-process, never via a `sleep` child. */
export function removeTemp(dir) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      rmSync(dir, { recursive: true, force: true });
      return;
    } catch (err) {
      if (err.code !== 'ENOTEMPTY' && err.code !== 'EBUSY') throw err;
      Atomics.wait(PAUSE, 0, 0, 50);
    }
  }
  rmSync(dir, { recursive: true, force: true, maxRetries: 5 });
}

/** A fresh fixture HOME with its 0700 test runtime root: `{ home, runtime }`. */
export function fixtureHome(prefix = 'mvt') {
  const home = realpathSync(mkdtempSync(`/tmp/${prefix}-`));
  const runtime = join(home, 'runtime');
  mkdirSync(runtime, { mode: 0o700 });
  return { home, runtime };
}

/** The environment of a fixture run rooted at `home` (see `fixtureHome`). */
export function fixtureEnv({ home, runtime }, extra = {}) {
  const env = { ...process.env };
  for (const key of ['QMD_SANDBOX', 'GEMINI_SANDBOX', 'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT']) delete env[key];
  return {
    ...env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime, MEMVANE_TEST_RUNTIME_ROOT: runtime,
    QMD_SETUP_GUARD_FIXTURE: '1', PYTHONDONTWRITEBYTECODE: '1', QMD_REAL_MODEL_TESTS: '', QMD_RECALL_LOG: '',
    GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0',
    ...extra,
  };
}
