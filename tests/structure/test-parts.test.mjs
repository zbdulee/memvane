// The split of the full suite into parts (tests/support/parts.mjs, scripts/test_part.mjs).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_SECONDS, PARTS, REST_SECONDS, TIERS, assign, parsePart, partFilter, pythonModules,
  runnerFiles } from '../support/parts.mjs';
import { recordedDurations, testModules } from '../support/runner.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
// The target of one part on the macOS CI job; the guard stops a part at 1,800 s.
const PART_TARGET_SECONDS = 1200;

test('a part is <k>/<n> with k inside n', () => {
  assert.deepEqual(parsePart('2/4'), { index: 2, count: 4 });
  assert.deepEqual(parsePart('1/1'), { index: 1, count: 1 });
  for (const bad of ['', undefined, '0/4', '5/4', '2', '2/0', 'a/b', '2/4/1', ' 2/4']) {
    assert.equal(parsePart(bad), null, String(bad));
  }
});

test('modules go longest first to the lightest part and the last part carries the rest', () => {
  const durations = { a: 50, b: 40, c: 30, d: 20 };
  const { parts, loads } = assign(['d', 'new', 'c', 'b', 'a', 'a'], durations, 3, 45);
  // a(50) -> 1, b(40) -> 2, c(30) -> 2 (40 < 45 < 50), d(20) -> 3 (45), new(10) -> 1 (50).
  assert.deepEqual(parts, [['a', 'new'], ['b', 'c'], ['d']]);
  assert.deepEqual(loads, [50 + DEFAULT_SECONDS, 70, 65]);
  assert.deepEqual(assign(['x'], {}, 1, 7), { parts: [['x']], loads: [7 + DEFAULT_SECONDS] });
});

test('every Python test module of the domain runners runs in exactly one part', () => {
  const modules = pythonModules(ROOT);
  const runners = runnerFiles(ROOT);
  assert.ok(runners.length >= 25, `runners ${runners.length}`);
  // What the runners start: the tier directories next to each run.test.mjs.
  const started = new Set();
  for (const runner of runners) {
    const source = readFileSync(join(ROOT, runner), 'utf8');
    const named = /tiers:\s*\[([^\]]*)\]/.exec(source);
    const tiers = named ? named[1].split(',').map((tier) => tier.trim().replace(/['"]/g, '')) : ['unit', 'integration'];
    for (const tier of tiers) {
      assert.ok(TIERS.includes(tier), `${runner}: tier ${tier} is not one parts.mjs splits`);
      for (const file of testModules(join(ROOT, dirname(runner), tier), ROOT)) started.add(file);
    }
  }
  // No runner starts a module the split does not know, and no tier module is left without a runner.
  assert.deepEqual([...started].sort(), modules);
  assert.ok(started.size >= 339, `modules ${started.size}`);
  const durations = recordedDurations();
  for (const count of [1, 2, 3, PARTS, PARTS + 1]) {
    const seen = new Map();
    for (let index = 1; index <= count; index += 1) {
      const inPart = partFilter(`${index}/${count}`, durations, ROOT);
      for (const module of started) if (inPart(module)) seen.set(module, (seen.get(module) || 0) + 1);
    }
    assert.equal(seen.size, started.size, `${count} parts cover every module`);
    assert.ok([...seen.values()].every((times) => times === 1), `${count} parts: no module twice`);
  }
  // Without a part every module runs; a module outside the tier directories runs in the last part.
  assert.ok(partFilter(undefined, durations, ROOT)('anything'));
  assert.ok(partFilter(`${PARTS}/${PARTS}`, durations, ROOT)('memvane/x/tests/other/test_y.py'));
  assert.ok(!partFilter(`1/${PARTS}`, durations, ROOT)('memvane/x/tests/other/test_y.py'));
});

test('the recorded durations keep every part of npm test under its target', () => {
  const { loads } = assign(pythonModules(ROOT), recordedDurations(), PARTS);
  assert.ok(REST_SECONDS > 0);
  for (const [index, seconds] of loads.entries()) {
    assert.ok(seconds <= PART_TARGET_SECONDS,
      `part ${index + 1}/${PARTS} records ${Math.round(seconds)} s: add a part (parts.mjs PARTS, package.json, the CI matrix)`);
  }
});

test('npm test and CI run the same parts', () => {
  const scripts = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).scripts;
  const names = Array.from({ length: PARTS }, (_, i) => `test:part${i + 1}`);
  assert.equal(scripts.test, names.map((name) => `npm run ${name}`).join(' && '));
  names.forEach((name, i) => assert.equal(scripts[name], `node scripts/test_part.mjs ${i + 1}/${PARTS}`));
  assert.equal(scripts[`test:part${PARTS + 1}`], undefined);
  const workflow = readFileSync(join(ROOT, '.github', 'workflows', 'test.yml'), 'utf8');
  const shards = `shard: [${names.map((_, i) => i + 1).join(', ')}]`;
  assert.equal(workflow.split(shards).length - 1, 2, 'both CI jobs run every part');
});
