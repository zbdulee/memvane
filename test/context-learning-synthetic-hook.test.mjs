import { test } from 'node:test';
import { execFileSync } from 'node:child_process';

test('synthetic edits require a real plugin index enqueue receipt', () => {
  execFileSync('python3', ['test/fixtures/context-learning-synthetic-hook-checks.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '' },
  });
});
