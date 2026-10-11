import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';

const code = [
  'import json, sys',
  'sys.path.insert(0, "core")',
  'import hook_budget',
  'print(json.dumps({"replay":hook_budget.deferred_replay(), "seconds":hook_budget._seconds()}))',
].join('\n');

function budget(extra = {}) {
  return JSON.parse(execFileSync('python3', ['-c', code], {
    encoding: 'utf8',
    env: {
      ...process.env, QMD_HOOK_TOTAL_SECONDS: '',
      QMD_EDIT_INTERNAL: '', QMD_DEFERRED_CAPTURE_REPLAY: '',
      QMD_DEFERRED_CAPTURE_TICKET: '', PYTHONDONTWRITEBYTECODE: '1',
      ...extra,
    },
  }));
}

test('only ticket-bound background replay gets a longer local budget', () => {
  const replay = {
    QMD_EDIT_INTERNAL: '1', QMD_DEFERRED_CAPTURE_REPLAY: '1',
    QMD_DEFERRED_CAPTURE_TICKET: 'a'.repeat(32),
  };
  assert.deepEqual(budget(), { replay: false, seconds: 18 });
  assert.deepEqual(budget({ QMD_HOOK_TOTAL_SECONDS: '55' }),
    { replay: false, seconds: 18 });
  assert.deepEqual(budget({ ...replay, QMD_DEFERRED_CAPTURE_TICKET: 'invalid' }),
    { replay: false, seconds: 18 });
  assert.deepEqual(budget(replay), { replay: true, seconds: 55 });
  assert.deepEqual(budget({ ...replay, QMD_HOOK_TOTAL_SECONDS: '60' }),
    { replay: true, seconds: 60 });
});

test('background deadline emits a fixed diagnostic while hook exit remains zero', () => {
  const run = [
    'import sys',
    'sys.path.insert(0, "core")',
    'import hook_budget, hook_main',
    'def expire(): raise hook_budget.HookDeadlineExceeded()',
    'print("exit", hook_main.run(expire))',
  ].join('\n');
  const result = spawnSync('python3', ['-c', run], {
    encoding: 'utf8',
    env: { ...process.env, QMD_RECALL_LOG: '',
      QMD_EDIT_INTERNAL: '1', QMD_DEFERRED_CAPTURE_REPLAY: '1',
      QMD_DEFERRED_CAPTURE_TICKET: 'a'.repeat(32),
      PYTHONDONTWRITEBYTECODE: '1' },
  });
  assert.equal(result.status, 0);
  assert.equal(result.stdout.trim(), 'exit 0');
  assert.equal(result.stderr.trim(), 'qmd_deferred_capture_deadline_exceeded');
});
