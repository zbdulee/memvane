import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const root = dirname(dirname(fileURLToPath(import.meta.url)));
test('QMD product cutover selection and recovery fences', () => {
  const run = spawnSync('python3', ['-B', join(root, 'test/fixtures/laya-qmd-cutover-checks.py')], {
    cwd: root, encoding: 'utf8', timeout: 30000,
    env: { ...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
  });
  if (run.status !== 0) throw new Error(`${run.stdout}\n${run.stderr}`);
});
