import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';

const checker = resolve('test/helpers/structure_guard.py');
const python = process.env.STRUCTURE_TEST_PYTHON || 'python3';

function fixture(t, legacyViolation = false, sameLayer = false) {
  const root = mkdtempSync(join(tmpdir(), 'memvane-structure-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  mkdirSync(join(root, 'core'));
  const files = {
    'core/base.py': legacyViolation ? 'import app\n' : 'VALUE = 1\n',
    'core/app.py': 'import base\n',
    'core/policy.py': 'from pathlib import PurePath\n',
    'core/entry.py': 'import app\n',
  };
  for (const [path, text] of Object.entries(files)) writeFileSync(join(root, path), text);
  const modules = {
    'core/base.py': {context: 'base', layer: sameLayer ? 'application' : 'adapter', pinned: true},
    'core/app.py': {context: 'app', layer: 'application', pinned: false},
    'core/policy.py': {context: 'base', layer: 'domain', pinned: false},
    'core/entry.py': {context: 'app', layer: 'entry', pinned: true},
  };
  const map = {contexts: {base: 0, app: 1}, modules, pinned_paths: ['core/base.py'],
    entrypoints: ['core/entry.py'], size_enforce_from: '2026-11-27'};
  writeFileSync(join(root, 'map.json'), JSON.stringify(map));
  const run = (...args) => {
    const result = spawnSync(python, ['-B', checker, '--root', root, '--map', join(root, 'map.json'), ...args],
      {encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
    assert.equal(result.error, undefined);
    let output;
    try { output = JSON.parse(result.stdout); } catch { assert.fail(`invalid output: ${result.stderr} ${result.stdout}`); }
    return {status: result.status, ...output};
  };
  const snap = run('snapshot');
  assert.equal(snap.status, 0, JSON.stringify(snap));
  writeFileSync(join(root, 'baseline.json'), JSON.stringify(snap.snapshot));
  return {root, run, check: (...args) => run('check', '--baseline', join(root, 'baseline.json'), ...args)};
}

test('baseline passes and an unclassified module fails', t => {
  const f = fixture(t);
  assert.equal(f.check().status, 0);
  writeFileSync(join(f.root, 'core/new.py'), 'pass\n');
  const result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('unclassified') && x.includes('core/new.py')));
});

test('new reverse context and layer import fails without masking old edges', t => {
  const f = fixture(t);
  writeFileSync(join(f.root, 'core/base.py'), 'import app\n');
  const result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('import_direction') && x.includes('core/base.py')));
});

test('context direction is enforced even when layers match', t => {
  const f = fixture(t, false, true);
  writeFileSync(join(f.root, 'core/base.py'), 'import app\n');
  assert.ok(f.check().errors.some(x => x.includes('import_direction:core/base.py')));
});

test('layer direction is enforced inside one context', t => {
  const f = fixture(t);
  writeFileSync(join(f.root, 'core/policy.py'), 'import base\n');
  assert.ok(f.check().errors.some(x => x.includes('import_direction:core/policy.py')));
});

test('an existing reverse import may remain but its count cannot grow', t => {
  const f = fixture(t, true);
  assert.equal(f.run('snapshot').snapshot.metrics.largest_top_scc, 2);
  assert.equal(f.check().status, 0);
  writeFileSync(join(f.root, 'core/base.py'), 'import app\nimport app\n');
  const result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('import_direction') && x.includes('1->2')));
});

test('existing classifications cannot be silently weakened', t => {
  const f = fixture(t);
  const mapPath = join(f.root, 'map.json');
  const map = JSON.parse(readFileSync(mapPath, 'utf8'));
  map.modules['core/policy.py'].layer = 'adapter';
  writeFileSync(mapPath, JSON.stringify(map));
  const result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('classification_changed:core/policy.py')));
});

test('domain I/O imports and calls fail even when lazy', t => {
  const f = fixture(t);
  writeFileSync(join(f.root, 'core/policy.py'), 'def f():\n    import sqlite3\n    return open("x")\n');
  const result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('domain_io') && x.includes('sqlite3')));
  assert.ok(result.errors.some(x => x.includes('domain_io') && x.includes('open')));
  writeFileSync(join(f.root, 'core/policy.py'), 'def f():\n    return __import__("socket")\n');
  assert.ok(f.check().errors.some(x => x.includes('domain_io') && x.includes('socket')));
});

test('top-level hook import closure and required paths are pinned', t => {
  const f = fixture(t);
  writeFileSync(join(f.root, 'core/app.py'), 'import base\nimport json\n');
  let result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('hook_closure') && x.includes('json')));
  writeFileSync(join(f.root, 'core/app.py'), 'import base\ndef lazy():\n    import json\n');
  assert.equal(f.check().status, 0, 'lazy import does not expand top-level closure');
  rmSync(join(f.root, 'core/base.py'));
  result = f.check();
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('pinned_path') && x.includes('core/base.py')));
});

test('file and function size increases report for two weeks then fail', t => {
  const f = fixture(t);
  writeFileSync(join(f.root, 'core/base.py'), 'def huge():\n' + '    pass\n'.repeat(151));
  let result = f.check('--today', '2026-11-20');
  assert.equal(result.status, 0);
  assert.ok(result.warnings.some(x => x.includes('function_size')));
  result = f.check('--today', '2026-11-28');
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('function_size')));
  writeFileSync(join(f.root, 'core/base.py'), '# line\n'.repeat(801));
  result = f.check('--today', '2026-11-20');
  assert.equal(result.status, 0);
  assert.ok(result.warnings.some(x => x.includes('file_size')));
  result = f.check('--today', '2026-11-28');
  assert.notEqual(result.status, 0);
  assert.ok(result.errors.some(x => x.includes('file_size')));
});

test('current mapped tree satisfies the ratchet baseline', () => {
  const result = spawnSync(python, ['-B', checker, '--root', '.', '--map', 'test/structure/context-map.json',
    'check', '--baseline', 'test/structure/baseline.json'],
    {encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.deepEqual(JSON.parse(result.stdout).errors, []);
});
