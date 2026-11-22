// Relative links of tracked Markdown documents resolve to a file in the repository.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
// Generated cards are not maintained documents.
const SKIPPED = /^\.memvane\//;

/** `[text](target)` targets of Markdown `text` that name a path (code spans and fences removed). */
export function relativeLinks(text) {
  const prose = text.replace(/```[\s\S]*?```/g, '').replace(/`[^`\n]*`/g, '');
  const found = [];
  for (const match of prose.matchAll(/\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g)) {
    const target = match[1].replace(/^<|>$/g, '');
    if (/^(?:[a-z][a-z0-9+.-]*:|#|\/\/)/i.test(target)) continue;
    found.push(target.split('#')[0].split('?')[0]);
  }
  return found.filter(Boolean);
}

test('the link reader keeps relative paths and drops addresses, anchors and code', () => {
  const text = ['[a](docs/a.md) [b](https://example.invalid/x) [c](#part) [d](../e.md#part)',
    '`[f](not/a/link.md)`', '```', '[g](fenced.md)', '```', '[h](mailto:x@example.invalid)'].join('\n');
  assert.deepEqual(relativeLinks(text), ['docs/a.md', '../e.md']);
});

test('every relative link of a tracked document resolves', () => {
  const files = execFileSync('git', ['-C', ROOT, 'ls-files', '-z', '*.md'], { encoding: 'utf8', maxBuffer: 64 << 20 })
    .split('\0').filter((file) => file && !SKIPPED.test(file));
  assert.ok(files.length > 30);
  const broken = [];
  for (const file of files) {
    for (const target of relativeLinks(readFileSync(join(ROOT, file), 'utf8'))) {
      let path;
      try {
        path = normalize(join(dirname(file), decodeURIComponent(target)));
      } catch {
        path = normalize(join(dirname(file), target));
      }
      if (path.startsWith('..') || !existsSync(join(ROOT, path))) broken.push(`${file} -> ${target}`);
    }
  }
  assert.deepEqual(broken, []);
});
