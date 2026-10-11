import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
test('real edit tool before/after content and delete preimage', () => {
  execFileSync('python3', [join(root, 'test/fixtures/edit-content-hook-checks.py')], {
    cwd: root, encoding: 'utf8', env: { ...process.env, QMD_RECALL_LOG: '',
      PYTHONPYCACHEPREFIX: '/tmp/qmd-edit-hook-test-pycache' },
  });
});
