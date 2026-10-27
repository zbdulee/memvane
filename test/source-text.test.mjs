import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

function python(code) {
  return execFileSync('python3', ['-B', '-c', `import sys; sys.path.insert(0, 'core')\n${code}`], {
    encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  }).trim();
}

test('embed worker mirrors the shared source-text extension allowlist', () => {
  const worker = readFileSync('core/laya_local_embed_worker.mjs', 'utf8');
  const match = worker.match(/const SOURCE_TEXT_EXTENSIONS = Object\.freeze\((\[[^\]]*\])\)/);
  assert.ok(match, 'worker declares SOURCE_TEXT_EXTENSIONS');
  const mirrored = JSON.parse(match[1].replaceAll("'", '"'));
  const shared = JSON.parse(python('import json, source_text; print(json.dumps(list(source_text.SOURCE_TEXT_EXTENSIONS)))'));
  assert.deepEqual(mirrored, shared);
});

test('source_text matches extensions case-insensitively and denies dependency directories', () => {
  const out = JSON.parse(python(`import json, source_text as t
names = ('a.md', 'A.TXT', 'b.Rst', 'c.adoc', 'd.mdx', 'e.markdown', 'f.py', 'g.json', 'md')
paths = ('docs/node_modules/x.md', 'build/a.md', 'docs/a.md', 'docs/builder/a.md')
print(json.dumps([t.is_source_text(n) for n in names] + [t.is_denied_source_path(p) for p in paths]))`));
  assert.deepEqual(out, [true, true, true, true, true, true, false, false, false,
                         true, true, false, false]);
});
