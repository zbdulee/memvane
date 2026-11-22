// One node test per Python test module of a domain, each in its own fixture HOME.
//
//   memvane/<domain>/tests/run.test.mjs:
//     import { runDomain } from '../../../tests/support/runner.mjs';
//     runDomain({ domain: 'store' });
//
// Every `test_*.py` under memvane/<domain>/tests/<tier> runs as
// `python3 -B -m unittest <dotted module> -v` with a new realpath'd
// HOME/TMPDIR, its 0700 test runtime root (MEMVANE_TEST_RUNTIME_ROOT),
// QMD_SETUP_GUARD_FIXTURE=1, PYTHONDONTWRITEBYTECODE=1 and QMD_REAL_MODEL_TESTS
// empty, so a failure names its module. A child killed by a signal reports it.
// Afterwards no process named by a pid file under the HOME may still run (it is
// killed and the module fails); the HOME is removed.
//
// A module's timeout is three times its recorded duration
// (tests/support/durations.json, written by scripts/test_durations.py), at
// least 60 s; 180 s without a record; all times the run's load scale
// (timing.mjs). With MEMVANE_TEST_RESULTS set, each
// module appends one JSON line {module, status, seconds, commit} to that file.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';
import { timeScale } from './timing.mjs';

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

export const MODULE_TIMEOUT_FLOOR_SECONDS = 60;
export const MODULE_TIMEOUT_UNMEASURED_SECONDS = 180;
export const MODULE_TIMEOUT_FACTOR = 3;

/** Timeout in ms of a module whose recorded duration is `seconds` (undefined: not recorded). */
export function moduleTimeoutMs(seconds, scale = 1) {
  const base = typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0
    ? Math.max(MODULE_TIMEOUT_FLOOR_SECONDS, seconds * MODULE_TIMEOUT_FACTOR)
    : MODULE_TIMEOUT_UNMEASURED_SECONDS;
  return Math.round(base * 1000 * (scale >= 1 ? scale : 1));
}

/** Recorded module durations: `{ 'memvane/<domain>/tests/<tier>/test_x.py': seconds }`. */
export function recordedDurations(path = join(ROOT, 'tests', 'support', 'durations.json')) {
  try {
    const value = JSON.parse(readFileSync(path, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

/** Sorted `test_*.py` files under `dir` (recursive), as paths relative to `root`. */
export function testModules(dir, root = ROOT) {
  const found = [];
  const walk = (current) => {
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const path = join(current, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile() && /^test_.*\.py$/.test(entry.name)) found.push(relative(root, path));
    }
  };
  walk(dir);
  return found.sort();
}

/** The dotted unittest name of a module file: path separators become dots, `.py` is dropped. */
export function moduleName(file) {
  return file.replace(/\.py$/, '').split(/[\\/]/).join('.');
}

function record(module, status, seconds) {
  const target = process.env.MEMVANE_TEST_RESULTS;
  if (!target) return;
  try {
    appendFileSync(target, `${JSON.stringify({ module, status, seconds,
      commit: process.env.MEMVANE_TEST_COMMIT || process.env.GITHUB_SHA || null })}\n`);
  } catch { /* a result record never fails a test */ }
}

/** Run one module in a fixture HOME; throws an assertion error naming what failed. */
export function runModule(file, { root = ROOT, timeoutMs, label = file } = {}) {
  const fixture = fixtureHome('mvd');
  const started = process.hrtime.bigint();
  let status = 'fail';
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', moduleName(file), '-v'],
      { cwd: root, encoding: 'utf8', timeout: timeoutMs, env: fixtureEnv(fixture) });
    if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
    const left = leftoverProcesses(fixture.home);
    for (const { pid } of left) {
      try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
    }
    assert.ifError(r.error);
    assert.equal(r.signal, null, `${label} killed by ${r.signal}`);
    // Python 3.12+ exits 5 when a module holds no test yet (a skeleton).
    const empty = r.status === 5 && /\nNO TESTS RAN\b/.test(r.stderr);
    assert.ok(r.status === 0 || empty, `${label} exited ${r.status}`);
    if (!empty) assert.match(r.stderr, /\nOK\b/);
    assert.deepEqual(left, [], 'processes left running under the fixture HOME');
    status = 'pass';
  } finally {
    record(file, status, Number(process.hrtime.bigint() - started) / 1e9);
    removeTemp(fixture.home);
  }
}

export function runDomain({ domain, tiers = ['unit', 'integration'] }) {
  const durations = recordedDurations();
  for (const tier of tiers) {
    const start = join('memvane', domain, 'tests', tier);
    const modules = testModules(join(ROOT, start));
    if (modules.length === 0) {
      test(`${domain} ${tier} python tests`, { skip: 'tier absent' }, () => {});
      continue;
    }
    for (const file of modules) {
      // The name starts `<domain> <tier> python `: package.json's part3 skips by it.
      const label = `${domain} ${tier} python ${relative(start, file)}`;
      test(label, () => runModule(file, { timeoutMs: moduleTimeoutMs(durations[file], timeScale()), label }));
    }
  }
}
