import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('fresh-first content SQLite cold reader: independent default and system Python producers', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'laya-physical-cold-')));
  const runtime = join(home, 'runtime');mkdirSync(runtime, { mode: 0o700 });
  try {
    const selectedOnly = process.env.MEMVANE_SELECTED_COLD_TEST === '1';
    const fullOnly = process.env.MEMVANE_SELECTED_COLD_FULL === '1';
    const sideOnly = process.env.MEMVANE_SELECTED_COLD_SIDE === '1';
    const legacyOnly = process.env.MEMVANE_SELECTED_COLD_LEGACY === '1';
    const args = ['-B', 'test/fixtures/laya-reader-physical-cold-checks.py'];
    if (legacyOnly) args.push('PhysicalColdChecks.test_system_python39_selected_cold_full_query_with_legacy_sidecar');
    else if (sideOnly) args.push('PhysicalColdChecks.test_system_python39_selected_cold_full_query_with_sealed_sidecar');
    else if (fullOnly) args.push('PhysicalColdChecks.test_system_python39_selected_cold_full_query_without_optional_sidecar');
    else if (selectedOnly) args.push('PhysicalColdChecks.test_system_python39_selected_cold_search_without_sidecars');
    const result = spawnSync('python3', args, {
      encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, TMPDIR: home, XDG_RUNTIME_DIR: runtime,
        MEMVANE_TEST_RUNTIME_ROOT: runtime, QMD_SETUP_GUARD_FIXTURE: '1', QMD_SANDBOX: '1',
        PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    if (result.stdout) process.stdout.write(result.stdout);
    assert.ifError(result.error);assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, (selectedOnly || fullOnly || sideOnly || legacyOnly) ? /Ran 1 test\b/ : /Ran 6 tests\b/);
    assert.match(result.stderr, /\nOK\b/);
  } finally { removeTemp(home); }
});
