import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
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
    'def c():\n    bundle_paths = {}\n    return bundle_paths.get\n');
  const {result} = run(t, 'free_names.py', [sample]);
  assert.equal(result.status, 1, result.stdout);
  const names = JSON.parse(result.stdout).findings.map(x => x.split(':').pop()).sort();
  assert.deepEqual(names, ['Path', 'bundle_paths.removed_name', 'json']);
});

test('product modules satisfy structure rules G1 to G7', t => {
  const {result} = run(t, '../../test/structure/v1_guard.py');
  const report = JSON.parse(result.stdout);
  for (const rule of ['G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7']) {
    assert.deepEqual(report.issues[rule], [], rule);
  }
});
