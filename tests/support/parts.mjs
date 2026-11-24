// The split of the full suite into parts of about equal recorded duration.
//
// A part is `<k>/<n>` (MEMVANE_TEST_PART, set by scripts/test_part.mjs). The
// units are the Python test modules the domain runners start
// (`memvane/**/tests/<tier>/**/test_*.py`); every other node test file runs in
// the last part, which starts with REST_SECONDS for them. Modules go, longest
// first, to the part with the smallest load so far. A module without a
// recorded duration (tests/support/durations.json) counts DEFAULT_SECONDS.
import { readdirSync } from 'node:fs';
import { basename, dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

export const TIERS = ['unit', 'integration', 'statemachine'];
export const DEFAULT_SECONDS = 10;
// The node test files outside the domain runners, measured on the macOS CI job.
export const REST_SECONDS = 1000;
export const PARTS = 5;

/** `{ index, count }` of a `<k>/<n>` part with 1 <= k <= n, or null. */
export function parsePart(value) {
  const match = /^([1-9][0-9]?)\/([1-9][0-9]?)$/.exec(String(value || ''));
  if (!match) return null;
  const index = Number(match[1]);
  const count = Number(match[2]);
  return index <= count ? { index, count } : null;
}

function walk(dir, visit) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) visit(path, entry, true) !== false && walk(path, visit);
    else if (entry.isFile()) visit(path, entry, false);
  }
}

// Every domain runner file (a `run.test.mjs` directly under a `tests` directory), sorted, relative to `root`.
export function runnerFiles(root = ROOT) {
  const found = [];
  walk(join(root, 'memvane'), (path, entry, isDir) => {
    if (!isDir && entry.name === 'run.test.mjs' && basename(dirname(path)) === 'tests') {
      found.push(relative(root, path).split(/[\\/]/).join('/'));
    }
  });
  return found.sort();
}

/** Every Python test module under a tier directory of a `tests` directory, sorted, relative to `root`. */
export function pythonModules(root = ROOT) {
  const found = [];
  walk(join(root, 'memvane'), (path, entry, isDir) => {
    if (isDir || !/^test_.*\.py$/.test(entry.name)) return;
    const parts = relative(root, path).split(/[\\/]/);
    const at = parts.lastIndexOf('tests');
    if (at >= 0 && TIERS.includes(parts[at + 1])) found.push(parts.join('/'));
  });
  return found.sort();
}

/** `{ parts: [[module, ...], ...], loads: [seconds, ...] }` for `count` parts. */
export function assign(modules, durations, count, restSeconds = REST_SECONDS) {
  const weight = (module) => {
    const seconds = durations[module];
    return typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0 ? seconds : DEFAULT_SECONDS;
  };
  const parts = Array.from({ length: count }, () => []);
  const loads = parts.map(() => 0);
  loads[count - 1] = restSeconds;
  const ordered = [...new Set(modules)].sort((a, b) => weight(b) - weight(a) || (a < b ? -1 : 1));
  for (const module of ordered) {
    let target = 0;
    for (let i = 1; i < count; i += 1) if (loads[i] < loads[target]) target = i;
    parts[target].push(module);
    loads[target] += weight(module);
  }
  return { parts, loads };
}

/** A predicate: does `module` run in the part `value` names? Always true without a part. */
export function partFilter(value, durations, root = ROOT) {
  const part = parsePart(value);
  if (!part) return () => true;
  const known = pythonModules(root);
  const mine = new Set(assign(known, durations, part.count).parts[part.index - 1]);
  const all = new Set(known);
  // A module outside the tier directories is in no part's list: the last part runs it.
  return (module) => mine.has(module) || (part.index === part.count && !all.has(module));
}
