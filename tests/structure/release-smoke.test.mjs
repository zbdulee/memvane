// The release smoke stays a maintenance tool, and the smoke machine stays unnamed in this repository.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const tracked = execFileSync('git', ['-C', ROOT, 'ls-files', '-z'], { encoding: 'utf8', maxBuffer: 64 << 20 })
  .split('\0').filter(Boolean);
const TEXT = /\.(py|mjs|js|json|md|yml|yaml|sh|txt|toml)$/;
// sha256 of the names that only the private run record may hold (the machine and its test account).
const PRIVATE_NAMES = new Set([
  'ecdf4f6e5c966ff8f53126b0d387e74531688f61332a9394e1d78977b3cf18b0',
  'ef8657d1473ec5255c18a54739a6b7da5714b526857c5b8a893baa902a4b5049',
]);

test('no product code imports the release smoke', () => {
  const product = tracked.filter((file) => /^(memvane|entry|hooks|hermes_adapter|skills)\//.test(file)
    && !file.split('/').includes('tests') && /\.(py|mjs|js|sh)$/.test(file));
  assert.ok(product.length > 100);
  const users = product.filter((file) => /release_smoke|smoke_fixture/.test(readFileSync(join(ROOT, file), 'utf8')));
  assert.deepEqual(users, []);
});

test('the smoke machine and its account are not named in tracked files', () => {
  const seen = new Set();
  const named = [];
  for (const file of tracked.filter((name) => TEXT.test(name) && !name.startsWith('.memvane/'))) {
    for (const word of readFileSync(join(ROOT, file), 'utf8').toLowerCase().match(/[a-z0-9][a-z0-9.-]{3,40}/g) || []) {
      if (seen.has(word)) continue;
      seen.add(word);
      if (PRIVATE_NAMES.has(createHash('sha256').update(word).digest('hex'))) named.push(file);
    }
  }
  assert.deepEqual(named, []);
  // The remote address is always given: the tool holds no default.
  const tool = readFileSync(join(ROOT, 'scripts', 'release_smoke.py'), 'utf8');
  assert.match(tool, /add_argument\('--remote', required=True/);
  assert.doesNotMatch(tool, /--remote'[^\n]*default=/);
});
