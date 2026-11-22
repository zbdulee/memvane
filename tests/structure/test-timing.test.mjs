// Test timing rules: the load scale stays a test-only input (tests/support/timing.mjs),
// and time-dependent test code does not grow.
//
// time-allowlist.json holds, per Python test file, how many lines sleep
// (`time.sleep(`), assert on elapsed time, or pass a literal timeout of five
// seconds or less. A file may not exceed its recorded counts; a file not listed
// may have none. Waits go through tests/support (`wait_until`, `scaled`), and
// budgets are asserted by counts and states. A count that went down passes: the
// record is lowered when the file's tests are next reworked.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SKIPPED = new Set(['node_modules', '.git', '__pycache__', '.memvane', 'docs']);

/** Repository files with one of `extensions`, as paths relative to the root. */
export function sourceFiles(extensions, dir = ROOT, found = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIPPED.has(entry.name)) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(extensions, path, found);
    else if (entry.isFile() && extensions.some((ext) => entry.name.endsWith(ext))) {
      found.push(relative(ROOT, path).split(sep).join('/'));
    }
  }
  return found.sort();
}

/** A file under a `tests` directory (or the repository's own `tests/`). */
export function isTestFile(file) {
  return file.split('/').includes('tests');
}

test('no product code reads the test load scale', () => {
  const readers = sourceFiles(['.py', '.mjs', '.js', '.sh', '.json'])
    .filter((file) => !isTestFile(file))
    .filter((file) => readFileSync(join(ROOT, file), 'utf8').includes('MEMVANE_TEST_TIME_SCALE'));
  assert.deepEqual(readers, []);
});

const ALLOWLIST = join(ROOT, 'tests', 'structure', 'time-allowlist.json');
// The helpers that implement the waits.
const HELPERS = new Set(['tests/support/case.py']);
const ELAPSED = /\b(assertLess|assertLessEqual|assertGreater|assertGreaterEqual|assertAlmostEqual)\(.*\b(elapsed|duration|took|monotonic\(\)|perf_counter\(\)|time\.time\(\))/;
const SHORT_TIMEOUT = /\btimeout\s*=\s*(\d+(?:\.\d+)?|\.\d+)\s*[,)]/g;

/** `{ sleep, elapsed, shortTimeout }` line counts of Python test source `text`. */
export function timeCounts(text) {
  const counts = { sleep: 0, elapsed: 0, shortTimeout: 0 };
  for (const line of text.split('\n')) {
    const code = line.replace(/#.*$/, '');
    if (/\btime\.sleep\(/.test(code)) counts.sleep += 1;
    if (ELAPSED.test(code)) counts.elapsed += 1;
    for (const match of code.matchAll(SHORT_TIMEOUT)) {
      if (Number(match[1]) <= 5) counts.shortTimeout += 1;
    }
  }
  return counts;
}

/** Counts of every Python test file that has any, keyed by path. */
export function measuredTimeCounts() {
  const found = {};
  for (const file of sourceFiles(['.py']).filter((name) => isTestFile(name) && !HELPERS.has(name))) {
    const counts = timeCounts(readFileSync(join(ROOT, file), 'utf8'));
    if (counts.sleep || counts.elapsed || counts.shortTimeout) found[file] = counts;
  }
  return found;
}

/** `file kind measured > allowed` for every count over its record. */
export function overAllowance(measured, allowed) {
  const over = [];
  for (const [file, counts] of Object.entries(measured)) {
    for (const [kind, count] of Object.entries(counts)) {
      const limit = allowed[file]?.[kind] ?? 0;
      if (count > limit) over.push(`${file} ${kind} ${count} > ${limit}`);
    }
  }
  return over.sort();
}

test('the time counter sees sleeps, elapsed assertions and short literal timeouts', () => {
  const text = [
    'time.sleep(0.1)  # wait',
    '# time.sleep(9)',
    'self.assertLess(elapsed, 2)',
    'self.assertLess(len(rows), 2)',
    'self.assertGreaterEqual(time.monotonic() - started, 1)',
    'subprocess.run(argv, timeout=5)',
    'subprocess.run(argv, timeout=5.5, check=True)',
    'run(a, timeout=.5); run(b, timeout=2)',
    'run(a, timeout=scaled(2))',
    'run(a, timeout=limit)',
  ].join('\n');
  assert.deepEqual(timeCounts(text), { sleep: 1, elapsed: 2, shortTimeout: 3 });
});

test('a count over its record is reported and a lower count is not', () => {
  const allowed = { 'a/tests/test_a.py': { sleep: 2, elapsed: 0, shortTimeout: 1 } };
  assert.deepEqual(overAllowance({ 'a/tests/test_a.py': { sleep: 1, elapsed: 0, shortTimeout: 1 } }, allowed), []);
  assert.deepEqual(overAllowance({
    'a/tests/test_a.py': { sleep: 3, elapsed: 0, shortTimeout: 1 },
    'b/tests/test_new.py': { sleep: 0, elapsed: 1, shortTimeout: 0 },
  }, allowed), ['a/tests/test_a.py sleep 3 > 2', 'b/tests/test_new.py elapsed 1 > 0']);
});

test('time-dependent test code stays within time-allowlist.json', () => {
  const allowed = JSON.parse(readFileSync(ALLOWLIST, 'utf8'));
  assert.deepEqual(overAllowance(measuredTimeCounts(), allowed), []);
});

