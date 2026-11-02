import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

test('format3 refuses an actual frozen format2 store before any write', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'blob-format3-home-')));
  try {
    const run = spawnSync('python3', ['-B', fileURLToPath(new URL(
      './fixtures/laya-blob-format3-checks.py', import.meta.url))], {
      encoding: 'utf8', timeout: 90000,
      env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: '', QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        QMD_RECALL_LOG: '', QMD_REAL_MODEL_TESTS: '' },
    });
    assert.ifError(run.error);
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.match(run.stderr, /Ran 4 tests\b/);
    assert.match(run.stderr, /\nOK\b/);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
