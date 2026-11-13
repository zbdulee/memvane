// Onboarding -> recall with the fixture provider (model-free stand-in for the real-model run).
// Each method runs in its own python process with a fresh temporary HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { removeTemp } from '../../test/helpers/temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SCRIPT = join(ROOT, 'tests', 'e2e', 'onboarding_fixture.py');
const METHODS = [
  'test_project_without_cards_onboards_and_recall_adds_no_card',
  'test_project_with_thirty_cards_recalls_the_matching_card',
];

for (const method of METHODS) {
  test(`fixture onboarding ${method}`, () => {
    // The per-install socket path is at most 100 bytes: a short HOME directly
    // under /tmp keeps it within that bound (and inside a platform temp root).
    const home = realpathSync(mkdtempSync('/tmp/mvf-'));
    const runtime = join(home, 'runtime');
    mkdirSync(runtime, { mode: 0o700 });
    try {
      const r = spawnSync('python3', ['-B', SCRIPT, `FixtureOnboarding.${method}`], {
        cwd: ROOT, encoding: 'utf8', timeout: 300000,
        env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
          MEMVANE_TEST_RUNTIME_ROOT: runtime, PYTHONDONTWRITEBYTECODE: '1',
          QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1', QMD_RECALL_LOG: '',
          GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_SYSTEM: '/dev/null', GIT_TERMINAL_PROMPT: '0' },
      });
      if (r.stdout) process.stdout.write(r.stdout);
      assert.ifError(r.error);
      assert.equal(r.signal, null, r.stderr);
      assert.equal(r.status, 0, r.stderr);
      assert.match(r.stderr, /Ran 1 test\b/);
      assert.match(r.stderr, /\nOK\b/);
    } finally {
      removeTemp(home);
    }
  });
}
