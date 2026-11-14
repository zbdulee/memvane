// S1: one node test per tier of a domain's Python tests, in a fixture HOME.
//
//   memvane/<domain>/tests/run.test.mjs:
//     import { runDomain } from '../../../tests/support/runner.mjs';
//     runDomain({ domain: 'store' });
//
// Each tier runs `python3 -B -m unittest discover -s memvane/<domain>/tests/<tier>
// -t . -p 'test_*.py'` with a new realpath'd HOME/TMPDIR, its 0700 test
// runtime root (MEMVANE_TEST_RUNTIME_ROOT), QMD_SETUP_GUARD_FIXTURE=1,
// PYTHONDONTWRITEBYTECODE=1 and QMD_REAL_MODEL_TESTS empty. A child killed
// by a signal reports it. Afterwards no process named by a pid file under
// the HOME may still run (it is killed and the tier fails); the HOME is removed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

function pidFiles(dir, found = []) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return found;
  }
  for (const entry of entries) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) pidFiles(path, found);
    else if (entry.isFile() && /\.pid(\.json)?$/.test(entry.name)) found.push(path);
  }
  return found;
}

function recordedPid(path) {
  try {
    const raw = readFileSync(path, 'utf8').trim();
    const value = raw.startsWith('{') ? JSON.parse(raw).pid : Number(raw);
    return Number.isInteger(value) && value > 1 ? value : null;
  } catch {
    return null;
  }
}

function commandOf(pid) {
  const r = spawnSync('ps', ['-p', String(pid), '-o', 'command='], { encoding: 'utf8' });
  return r.status === 0 ? r.stdout.trim() : null;
}

/** Live processes a pid file under `home` names whose command line mentions `home` (pid reuse is ignored). */
export function leftoverProcesses(home) {
  const alive = [];
  for (const file of pidFiles(home)) {
    const pid = recordedPid(file);
    if (pid === null || pid === process.pid) continue;
    const command = commandOf(pid);
    if (command && command.includes(home)) alive.push({ pid, file, command });
  }
  return alive;
}

// Per-tier child timeout. A domain whose slowest tier, run alone under the
// development guard, takes more than two thirds of the default gets its own
// value with at least 50% headroom over that measurement (S5 L13).
export const DEFAULT_TIER_TIMEOUT_MS = 600000;
// Measured alone (2026-11-15): sync integration 390 s, registry integration 423 s.
export const DOMAIN_TIER_TIMEOUT_MS = {
  registry: 900000,
  sync: 900000,
};

export function runDomain({ domain, tiers = ['unit', 'integration'],
  timeoutMs = DOMAIN_TIER_TIMEOUT_MS[domain] ?? DEFAULT_TIER_TIMEOUT_MS }) {
  for (const tier of tiers) {
    const start = join('memvane', domain, 'tests', tier);
    test(`${domain} ${tier} python tests`, { skip: !existsSync(join(ROOT, start)) && 'tier absent' }, () => {
      const fixture = fixtureHome('mvd');
      try {
        const r = spawnSync('python3', ['-B', '-m', 'unittest', 'discover', '-s', start, '-t', '.',
          '-p', 'test_*.py', '-v'], { cwd: ROOT, encoding: 'utf8', timeout: timeoutMs, env: fixtureEnv(fixture) });
        if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
        const left = leftoverProcesses(fixture.home);
        for (const { pid } of left) {
          try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
        }
        assert.ifError(r.error);
        assert.equal(r.signal, null, `${domain} ${tier} killed by ${r.signal}`);
        // Python 3.12+ exits 5 when a tier holds no test yet (a skeleton).
        const empty = r.status === 5 && /\nNO TESTS RAN\b/.test(r.stderr);
        assert.ok(r.status === 0 || empty, `${domain} ${tier} exited ${r.status}`);
        if (!empty) assert.match(r.stderr, /\nOK\b/);
        assert.deepEqual(left, [], 'processes left running under the fixture HOME');
      } finally {
        removeTemp(fixture.home);
      }
    });
  }
}
