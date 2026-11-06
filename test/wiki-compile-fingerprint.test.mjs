import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('compile worker binds observed source fingerprints to its single source read', () => {
  // Real temporary git repos and the real checkout capture: tracked, untracked,
  // staged-new, unborn, detached, non-git and git failure/timeout rows; a checkout
  // change during the read holds the job before any paid extraction; model
  // fingerprint fields are replaced; card and verify job bytes are unchanged.
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-fingerprint-home-')));
  try {
    const run = spawnSync('python3', ['-B', 'test/fixtures/wiki-compile-fingerprint-checks.py'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 120000,
      env: { ...process.env, HOME: home, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
    });
    assert.equal(run.status, 0, run.stderr || run.stdout);
  } finally { removeTemp(home); }
});
