import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('install event log: writers, schema, privacy, rotation, refusals, latency and logs skill', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'memvane-events-home-')));
  try {
    const result = spawnSync('python3', ['-B', 'test/fixtures/event-log-checks.py'], {
      encoding: 'utf8', timeout: 120000,
      env: {...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1',
            MEMVANE_EVENT_LOG_PROMPTS: ''},
    });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.match(result.stderr, /Ran 34 tests/);
    assert.match(result.stderr, /\nOK\b/);
  } finally {
    removeTemp(home);
  }
});
