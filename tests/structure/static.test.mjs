import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';

const python = process.env.STRUCTURE_TEST_PYTHON || 'python3';
const here = resolve('tests/structure');

function run(t, script, args = []) {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'mv-static-')));
  t.after(() => rmSync(home, {recursive: true, force: true}));
  const env = {PATH: process.env.PATH, HOME: home, TMPDIR: home, PYTHONDONTWRITEBYTECODE: '1',
    QMD_SETUP_GUARD_FIXTURE: '1'};
  const result = spawnSync(python, ['-B', join(here, script), ...args],
    {encoding: 'utf8', env, timeout: 300000});
  assert.equal(result.error, undefined);
  assert.equal(result.signal, null, `${script} timed out`);
  return {home, result};
}

test('every product module and entry script imports under a synthetic HOME', t => {
  const {home, result} = run(t, 'import_all.py');
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const report = JSON.parse(result.stdout);
  assert.deepEqual(report.failures, {});
  assert.ok(report.modules > 250, `only ${report.modules} modules found`);
  assert.deepEqual(readdirSync(home), [], 'importing must not write under HOME');
});

test('no product module reads a name that no scope binds', t => {
  const {result} = run(t, 'free_names.py');
  assert.equal(result.status, 0, result.stdout);
  const report = JSON.parse(result.stdout);
  assert.deepEqual(report.findings, []);
  assert.deepEqual(report.unchecked, []);
  assert.ok(report.files > 250, `only ${report.files} files checked`);
});

test('the free-name check flags unbound names and missing module attributes', t => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'mv-free-')));
  t.after(() => rmSync(dir, {recursive: true, force: true}));
  const sample = join(dir, 'sample.py');
  writeFileSync(sample, 'from memvane.kernel import bundle_paths\n\n' +
    'def a():\n    from pathlib import Path\n    return Path, bundle_paths.entry_path\n\n' +
    'def b(config):\n    return Path(1), json, config.get, bundle_paths.removed_name\n\n' +
    'def c():\n    bundle_paths = {}\n    return bundle_paths.get\n\n' +
    'def d():\n    from memvane.runtime import api as build\n    return build.resource_report, build.removed_op\n\n' +
    'def e():\n    import os\n    from memvane.runtime import api as os\n    return os.getcwd\n\n' +
    'def f():\n    from memvane.journal import journal_reader as reader\n' +
    '    return reader.journal.inspect_journal, reader.journal.removed_row\n');
  const {result} = run(t, 'free_names.py', [sample]);
  assert.equal(result.status, 1, result.stdout);
  const names = JSON.parse(result.stdout).findings.map(x => x.split(':').pop()).sort();
  assert.deepEqual(names, ['Path', 'build.removed_op', 'bundle_paths.removed_name', 'json', 'os.getcwd',
    'reader.journal.removed_row']);
});

test('product modules satisfy structure rules G1 to G7 and strict G1, G5, G9', t => {
  const {result} = run(t, 'v1_guard.py', ['--strict']);
  const report = JSON.parse(result.stdout);
  for (const rule of ['G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7']) {
    assert.deepEqual(report.issues[rule], [], rule);
  }
  for (const rule of ['G1', 'G5', 'G9']) {
    assert.deepEqual(report.strict_issues[rule], [], `strict ${rule}`);
  }
  // Each api name limit exception is pinned at the domain's exact count.
  const {api_attributes: names, api_name_limits: limits} = report.strict_metrics;
  assert.deepEqual(limits, {journal: 51, runtime: 41, 'wiki/workspace': 35, 'wiki/card': 48});
  for (const [domain, limit] of Object.entries(limits)) {
    assert.equal(names[`memvane/${domain}/api.py`], limit, domain);
  }
});

// Report-only until the last S5 layer: old tests still carry G8 findings and
// tests/ still names internal modules. The counts are printed, not asserted.
test('test files report G8 and test G1 counts (report only)', t => {
  const {result} = run(t, 'v1_guard.py', ['--tests', '--rules', 'G1,G8']);
  const {counts, by_dir: folders} = JSON.parse(result.stdout).tests;
  for (const key of ['G1', 'G8', 'unresolved_patch_targets', 'external_patch_targets']) {
    assert.equal(typeof counts[key], 'number', key);
  }
  t.diagnostic(`test G1 ${counts.G1}, G8 ${counts.G8}, unresolved patch targets ${counts.unresolved_patch_targets}`);
  for (const [folder, row] of Object.entries(folders).sort()) {
    t.diagnostic(`${folder}: G1 ${row.G1}, G8 ${row.G8}, unresolved ${row.unresolved_patch_targets}`);
  }
});

// No hook or product entry sets the test runtime root: only a test harness
// does, so the fixture provider seam stays off for every installed run.
// Reads (os.environ.get, membership, copying an inherited value into a child
// env) are allowed. Line-based: a write that builds the name at run time is
// not seen; a module-level alias of the literal name is followed by name.
const TEST_ROOT = 'MEMVANE_TEST_RUNTIME_ROOT';
const SCANNED = new Set(['', '.py', '.mjs', '.js', '.sh', '.json']);

function productFiles() {
  const files = [];
  const walk = (dir, scripts) => {
    for (const entry of readdirSync(dir, {withFileTypes: true})) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!['tests', '__pycache__', 'node_modules'].includes(entry.name)) walk(path, scripts);
      } else if (entry.isFile() && SCANNED.has(extname(entry.name))
                 && (!scripts || path.split('/').includes('scripts'))) {
        files.push(path);
      }
    }
  };
  for (const dir of ['hooks', 'entry', 'hermes_adapter', 'memvane']) walk(dir, false);
  walk('skills', true);
  return files;
}

function testRootWrites(sources) {
  const quoted = `['"]${TEST_ROOT}['"]`;
  const aliases = new Set();
  for (const text of Object.values(sources)) {
    for (const m of text.matchAll(new RegExp(`^\\s*(\\w+)\\s*=\\s*${quoted}\\s*$`, 'gm'))) aliases.add(m[1]);
  }
  const keys = [quoted, ...[...aliases].map(name => `\\b${name}\\b`)];
  const rules = [];
  for (const key of keys) {
    rules.push(new RegExp(`\\[\\s*${key}\\s*\\]\\s*=(?!=)`),          // environ[KEY] = / env[KEY] =
               new RegExp(`\\b(?:setdefault|putenv)\\(\\s*${key}`),    // setdefault(KEY, ...) / putenv
               new RegExp(`[{,(]\\s*${key}\\s*:`));                   // {KEY: ...} env dict literal
  }
  rules.push(new RegExp(`(?<![\\w.'"])${TEST_ROOT}\\s*=(?!=)`),        // VAR= prefix, export VAR=, kwarg, env.VAR =
             new RegExp(`\\.${TEST_ROOT}\\s*=(?!=)`),                 // process.env.VAR =
             new RegExp(`\\bexport\\s+${TEST_ROOT}\\b`),
             new RegExp(`(?:^|[{,])\\s*${TEST_ROOT}\\s*:`));              // unquoted object key
  const found = [];
  for (const [file, text] of Object.entries(sources)) {
    text.split('\n').forEach((line, index) => {
      if (rules.some(rule => rule.test(line))) found.push(`${file}:${index + 1}: ${line.trim()}`);
    });
  }
  return found;
}

test('no hook or product entry sets the test runtime root', () => {
  const files = productFiles();
  assert.ok(files.length > 300, `only ${files.length} files scanned`);
  assert.ok(files.includes('hooks/run-hook') && files.some(f => f.startsWith('skills/')), 'scan roots');
  const sources = Object.fromEntries(files.map(file => [file, readFileSync(file, 'utf8')]));
  assert.deepEqual(testRootWrites(sources), []);
});

test('the test runtime root write check flags every write form and no read', () => {
  const writes = [
    "os.environ['MEMVANE_TEST_RUNTIME_ROOT'] = root",
    'env["MEMVANE_TEST_RUNTIME_ROOT"]=str(root)',
    "os.environ.setdefault('MEMVANE_TEST_RUNTIME_ROOT', root)",
    "os.putenv('MEMVANE_TEST_RUNTIME_ROOT', root)",
    "env = {'PATH': path, 'MEMVANE_TEST_RUNTIME_ROOT': root}",
    'env.update(MEMVANE_TEST_RUNTIME_ROOT=root)',
    'MEMVANE_TEST_RUNTIME_ROOT=/tmp/x python3 -B entry/run.py',
    'export MEMVANE_TEST_RUNTIME_ROOT',
    'process.env.MEMVANE_TEST_RUNTIME_ROOT = root;',
    'const env = {...process.env, MEMVANE_TEST_RUNTIME_ROOT: root};',
    '  MEMVANE_TEST_RUNTIME_ROOT: root,',
    'os.environ[ROOT_ENV] = root',
  ];
  const reads = [
    "raw = os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')",
    "if 'MEMVANE_TEST_RUNTIME_ROOT' in os.environ:",
    "env = {key: os.environ[key] for key in ('HOME', 'MEMVANE_TEST_RUNTIME_ROOT') if key in os.environ}",
    "if os.environ.get('MEMVANE_TEST_RUNTIME_ROOT') == root:",
    'while ``MEMVANE_TEST_RUNTIME_ROOT`` names an absolute path',
    'raw = os.environ.get(ROOT_ENV)',
  ];
  const sources = {'alias.py': "ROOT_ENV = 'MEMVANE_TEST_RUNTIME_ROOT'\n"};
  writes.forEach((line, i) => { sources[`w${i}`] = line; });
  reads.forEach((line, i) => { sources[`r${i}`] = line; });
  const flagged = testRootWrites(sources).map(row => row.split(':')[0]).sort();
  assert.deepEqual(flagged, writes.map((_line, i) => `w${i}`).sort());
});
