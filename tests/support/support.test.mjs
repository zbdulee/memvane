// The shared fixtures in tests/support (test_support.py), in a fixture HOME.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { leftoverProcesses, moduleName, moduleTimeoutMs, recordedDurations, resultCommit, runModule,
  testModules } from './runner.mjs';
import { fixtureEnv, fixtureHome, removeTemp } from './temp.mjs';
import { REFERENCE_START_MS, TIME_SCALE_ENV, clampScale, scaleOf, timeScale } from './timing.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

test('tests/support fixtures', () => {
  const fixture = fixtureHome('mvs');
  try {
    const r = spawnSync('python3', ['-B', '-m', 'unittest', 'tests.support.test_support', '-v'],
      { cwd: ROOT, encoding: 'utf8', timeout: 600000, env: fixtureEnv(fixture) });
    if (r.stderr) process.stderr.write(r.stderr.slice(-12000));
    assert.ifError(r.error);
    assert.equal(r.signal, null);
    assert.equal(r.status, 0);
    assert.match(r.stderr, /\nOK\b/);
    assert.deepEqual(leftoverProcesses(fixture.home), []);
  } finally {
    removeTemp(fixture.home);
  }
});

test('wiki trust probe reads a card the project builder wrote', async () => {
  const { wikiTrustState } = await import('./wiki_trust.mjs');
  const fixture = fixtureHome('mvw');
  try {
    const env = fixtureEnv(fixture);
    const made = spawnSync('python3', ['-B', '-c', [
      'import sys',
      'from tests.support.project import make_project',
      'p = make_project(sys.argv[1], name="trust")',
      'n = p.write_note("notes/a.md", "The amber lantern glows.\\n")',
      'p.write_card("concepts/a.md", title="Amber", body="The amber lantern glows.\\n", sources=[n])',
      'print(p.root)',
    ].join('\n'), fixture.home], { cwd: ROOT, encoding: 'utf8', env });
    assert.equal(made.status, 0, made.stderr);
    const project = made.stdout.trim();
    const previous = process.env.HOME;
    process.env.HOME = fixture.home;
    try {
      const state = wikiTrustState(project, '.memvane/wiki/concepts/a.md');
      assert.equal(state.status, 'verified');
      assert.equal(state.trusted, true);
      assert.equal(state.revisions.length, 1);
    } finally {
      process.env.HOME = previous;
    }
  } finally {
    removeTemp(fixture.home);
  }
});

test('module timeout: three times the record, 60 s floor, 180 s unrecorded, scaled', () => {
  assert.equal(moduleTimeoutMs(100), 300000);
  assert.equal(moduleTimeoutMs(4), 60000);
  assert.equal(moduleTimeoutMs(0), 60000);
  assert.equal(moduleTimeoutMs(undefined), 180000);
  assert.equal(moduleTimeoutMs('100'), 180000);
  assert.equal(moduleTimeoutMs(100, 2.5), 750000);
  assert.equal(moduleTimeoutMs(undefined, 4), 720000);
  // A scale under one never shortens a timeout.
  assert.equal(moduleTimeoutMs(100, 0.5), 300000);
  assert.deepEqual(recordedDurations('/nonexistent/durations.json'), {});
});

test('a failing module is reported under its own name and the other module passes', () => {
  const fixture = fixtureHome('mvr');
  const saved = process.env.MEMVANE_TEST_RESULTS;
  try {
    const root = join(fixture.home, 'tree');
    const tier = join(root, 'pkg', 'tests', 'unit');
    mkdirSync(join(tier, 'deep'), { recursive: true });
    for (const dir of ['pkg', 'pkg/tests', 'pkg/tests/unit', 'pkg/tests/unit/deep']) {
      writeFileSync(join(root, dir, '__init__.py'), '');
    }
    const body = (ok) => `import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(${ok})\n`;
    writeFileSync(join(tier, 'test_b_bad.py'), body('False'));
    writeFileSync(join(tier, 'test_a_ok.py'), body('True'));
    writeFileSync(join(tier, 'deep', 'test_c_ok.py'), body('True'));
    writeFileSync(join(tier, 'helper.py'), 'raise SystemExit(3)\n');
    const modules = testModules(tier, root);
    assert.deepEqual(modules, ['pkg/tests/unit/deep/test_c_ok.py', 'pkg/tests/unit/test_a_ok.py',
      'pkg/tests/unit/test_b_bad.py']);
    assert.equal(moduleName(modules[0]), 'pkg.tests.unit.deep.test_c_ok');
    const results = join(fixture.home, 'results.jsonl');
    process.env.MEMVANE_TEST_RESULTS = results;
    const outcome = modules.map((file) => {
      try {
        runModule(file, { root, timeoutMs: 60000, label: `pkg unit python ${file}` });
        return 'pass';
      } catch (err) {
        assert.match(String(err.message), /pkg unit python pkg\/tests\/unit\/test_b_bad\.py exited 1/);
        return 'fail';
      }
    });
    assert.deepEqual(outcome, ['pass', 'pass', 'fail']);
    const rows = readFileSync(results, 'utf8').trim().split('\n').map((line) => JSON.parse(line));
    assert.deepEqual(rows.map((row) => [row.module, row.status]),
      modules.map((file, index) => [file, outcome[index]]));
    assert.ok(rows.every((row) => typeof row.seconds === 'number' && row.seconds >= 0));
    // Each row carries the run's load scale and the commit of this checkout.
    assert.ok(rows.every((row) => row.scale === timeScale() && row.commit === resultCommit()));
    assert.match(resultCommit(), /^[0-9a-f]{40}$/);
    assert.equal(resultCommit({ MEMVANE_TEST_COMMIT: 'pr-head' }), 'pr-head');
  } finally {
    if (saved === undefined) delete process.env.MEMVANE_TEST_RESULTS;
    else process.env.MEMVANE_TEST_RESULTS = saved;
    removeTemp(fixture.home);
  }
});

test('test_durations.py writes the median of passing rows per module', () => {
  const fixture = fixtureHome('mvu');
  try {
    const rows = [['a/test_a.py', 'pass', 2], ['a/test_a.py', 'pass', 10], ['a/test_a.py', 'pass', 4],
      ['a/test_a.py', 'fail', 900], ['a/test_b.py', 'fail', 1], ['a/test_c.py', 'pass', 0.26]];
    const first = join(fixture.home, 'one.jsonl');
    const second = join(fixture.home, 'two.jsonl');
    const line = ([module, status, seconds]) => JSON.stringify({ module, status, seconds, commit: null });
    writeFileSync(first, `${rows.slice(0, 3).map(line).join('\n')}\nnot json\n`);
    writeFileSync(second, `${rows.slice(3).map(line).join('\n')}\n`);
    const out = join(fixture.home, 'durations.json');
    const r = spawnSync('python3', ['-B', join(ROOT, 'scripts', 'test_durations.py'), first, second, '--out', out],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(readFileSync(out, 'utf8')), { 'a/test_a.py': 4, 'a/test_c.py': 0.3 });
    assert.deepEqual(recordedDurations(out), { 'a/test_a.py': 4, 'a/test_c.py': 0.3 });
    writeFileSync(first, '');
    const none = spawnSync('python3', ['-B', join(ROOT, 'scripts', 'test_durations.py'), first, '--out', out],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    assert.equal(none.status, 1);
  } finally {
    removeTemp(fixture.home);
  }
});

test('the load scale is the median start time over the reference, kept in 1.0 to 4.0', () => {
  const ms = REFERENCE_START_MS;
  assert.equal(scaleOf([ms, ms, ms, ms, ms]), 1);
  assert.equal(scaleOf([ms * 0.5, ms * 0.5, ms * 0.5]), 1);
  // The median, not the mean: two slow starts out of five do not move it.
  assert.equal(scaleOf([ms, ms * 9, ms, ms * 9, ms]), 1);
  assert.equal(scaleOf([ms * 2, ms * 2.5, ms * 3]), 2.5);
  assert.equal(scaleOf([ms * 40, ms * 40, ms * 40]), 4);
  assert.equal(scaleOf([]), 1);
  assert.deepEqual(['2.5', '0.1', '99', 'x', ''].map(clampScale), [2.5, 1, 4, 1, 1]);
  // A value already in the environment is kept, and every fixture child receives the run's value.
  assert.equal(timeScale({ [TIME_SCALE_ENV]: '3' }), 3);
  const value = Number(fixtureEnv({ home: '/tmp/x', runtime: '/tmp/x/runtime' })[TIME_SCALE_ENV]);
  assert.ok(value >= 1 && value <= 4, String(value));
  assert.equal(value, timeScale());
});

test('test_flaky_report.py lists a module whose result changed within one commit', () => {
  const fixture = fixtureHome('mvf');
  try {
    const rows = [['a/test_a.py', 'pass', 'c1'], ['a/test_a.py', 'fail', 'c1'], ['a/test_a.py', 'pass', 'c1'],
      ['a/test_b.py', 'fail', 'c1'], ['a/test_b.py', 'pass', 'c2'], ['a/test_c.py', 'fail', null]];
    const file = join(fixture.home, 'results.jsonl');
    writeFileSync(file, `${rows.map(([module, status, commit]) =>
      JSON.stringify({ module, status, seconds: 1, commit })).join('\n')}\nnot json\n`);
    const r = spawnSync('python3', ['-B', join(ROOT, 'scripts', 'test_flaky_report.py'), file],
      { encoding: 'utf8', timeout: 60000, env: fixtureEnv(fixture) });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), { rows: 6, rowsWithoutCommit: 1, moduleCommits: 3,
      changed: [{ module: 'a/test_a.py', commit: 'c1', pass: 2, fail: 1 }] });
    assert.deepEqual(readFileSync(file, 'utf8').split('\n').length, 8);
  } finally {
    removeTemp(fixture.home);
  }
});

