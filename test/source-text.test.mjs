import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function python(code) {
  return execFileSync('python3', ['-B', '-c', `import sys; sys.path.insert(0, 'core')\n${code}`], {
    encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  }).trim();
}

test('source_text matches extensions case-insensitively and denies dependency and cache directories', () => {
  const out = JSON.parse(python(`import json, source_text as t
names = ('a.md', 'A.TXT', 'b.Rst', 'c.adoc', 'd.mdx', 'e.markdown', 'f.py', 'g.json', 'md')
paths = ('docs/node_modules/x.md', 'build/a.md', 'app/Library/PackageCache/x.md', 'app/Temp/x.md',
         'app/obj/x.md', 'docs/a.md', 'docs/builder/a.md', 'docs/library/a.md')
print(json.dumps([t.is_source_text(n) for n in names] + [t.is_denied_source_path(p) for p in paths]))`));
  assert.deepEqual(out, [true, true, true, true, true, true, false, false, false,
                         true, true, true, true, true, false, false, false]);
});
