import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';

const code = [
  'import json, sys',
  'sys.path.insert(0, "core")',
  'import hook_budget',
  'print(json.dumps({"seconds":hook_budget._seconds()}))',
].join('\n');

function seconds(extra = {}) {
  return JSON.parse(execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: { ...process.env, QMD_HOOK_TOTAL_SECONDS: '', PYTHONDONTWRITEBYTECODE: '1', ...extra },
  })).seconds;
}

test('hook budget accepts 2-20 seconds and falls back to 18', () => {
  assert.equal(seconds(), 18);
  assert.equal(seconds({ QMD_HOOK_TOTAL_SECONDS: '7' }), 7);
  assert.equal(seconds({ QMD_HOOK_TOTAL_SECONDS: '55' }), 18);
  assert.equal(seconds({ QMD_HOOK_TOTAL_SECONDS: '1' }), 18);
  assert.equal(seconds({ QMD_HOOK_TOTAL_SECONDS: 'nan' }), 18);
});

test('an exhausted hook deadline still exits zero without stderr', () => {
  const run = [
    'import sys',
    'sys.path.insert(0, "core")',
    'import hook_budget, hook_main',
    'def expire(): raise hook_budget.HookDeadlineExceeded()',
    'print("exit", hook_main.run(expire))',
  ].join('\n');
  const result = spawnSync('python3', ['-c', run], {
    encoding: 'utf8',
    env: { ...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), 'exit 0');
  assert.equal(result.stderr.trim(), '');
});
