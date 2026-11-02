import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

test('release coordination retains existing lock ownership and gates fresh workers', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'release-coordination-home-')));
  chmodSync(home, 0o700);
  try {
    const result = spawnSync('python3', [
      '-B', fileURLToPath(new URL('./fixtures/release-coordination-checks.py', import.meta.url)),
    ], {
      encoding: 'utf8',
      timeout: 90000,
      env: {
        ...process.env,
        HOME: home,
        XDG_CONFIG_HOME: join(home, '.config'),
        XDG_CACHE_HOME: join(home, '.cache'),
        XDG_STATE_HOME: join(home, '.local', 'state'),
        PYTHONDONTWRITEBYTECODE: '1',
        PYTHONPATH: '',
        QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SANDBOX: '1',
        QMD_RECALL_LOG: '',
      },
    });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 10 tests\b/);
    assert.match(result.stderr, /\nOK\b/);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
