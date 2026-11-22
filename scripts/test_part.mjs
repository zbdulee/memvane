#!/usr/bin/env node
// One part of the full suite, under the development guard.
//
//   node scripts/test_part.mjs <k>/<n>     run part k of n (exclusive, 1,800 s)
//   node scripts/test_part.mjs --plan [n]  print the recorded seconds of each part
//
// Parts 1..n-1 run the domain runners only; part n runs every node test file.
// In each, a runner starts just the Python modules of that part
// (tests/support/parts.mjs).
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PARTS, assign, parsePart, pythonModules, runnerFiles } from '../tests/support/parts.mjs';
import { recordedDurations } from '../tests/support/runner.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);

if (args[0] === '--plan') {
  const count = Number(args[1] || PARTS);
  const { parts, loads } = assign(pythonModules(ROOT), recordedDurations(), count);
  parts.forEach((modules, i) => {
    process.stdout.write(`part ${i + 1}/${count}: ${modules.length} modules, ${Math.round(loads[i])} s\n`);
  });
  process.exit(0);
}

const part = parsePart(args[0]);
if (!part) {
  process.stderr.write('usage: node scripts/test_part.mjs <k>/<n> | --plan [n]\n');
  process.exit(2);
}
const files = part.index === part.count ? [] : runnerFiles(ROOT);
const concurrency = process.env.MEMVANE_TEST_CONCURRENCY || '3';
const result = spawnSync('python3', ['-B', 'scripts/development_guard.py', '--kind', 'test', '--exclusive',
  '--max-seconds', '1800', '--', 'node', '--test', `--test-concurrency=${concurrency}`, ...files], {
  cwd: ROOT,
  stdio: 'inherit',
  env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '1', QMD_REAL_MODEL_TESTS: '',
    MEMVANE_TEST_PART: `${part.index}/${part.count}` },
});
if (result.error) throw result.error;
process.exit(result.status === null ? 1 : result.status);
