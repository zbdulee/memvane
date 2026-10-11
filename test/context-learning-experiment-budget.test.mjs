import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

for (const mutation of ['0', '1']) test(`synthetic launcher binds hook captures and checks final mutation ${mutation}`, () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-experiment-budget-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1', QMD_FINAL_MUTATION: mutation},
      encoding: 'utf8', timeout: 60000,
    });
  const report = JSON.parse(raw);
  assert.equal(report.pilot_budget, true);
  assert.equal(report.batch_restart, true);
  assert.equal(report.final_sealed, true);
  assert.equal(report.real_hook_uuid, true);
  assert.equal(report.late_cycle, true);
  assert.equal(report.final_gold_mutation, true);
  assert.equal(report.external_calls, 0);
});
