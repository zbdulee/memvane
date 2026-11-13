import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

for (const [name, fixture] of [
  ['selection gold, exact compact body, quality cadence and promotion policy', 'context-learning-cycle-checks.py'],
  ['200-question offline train, validation, evaluation, resume and rollback', 'context-learning-cycle-e2e.py'],
  ['project-bound teacher budget and separate creation/review models', 'context-learning-teacher-budget-checks.py'],
  ['read-only Laya runtime discovery and synthetic compatibility gate', 'context-learning-runtime-setup-checks.py'],
  ['managed Laya generation stage, activate and rollback without installs', 'context-learning-runtime-installer-checks.py'],
]) {
  test(name, () => {
    const raw = execFileSync('python3', [`test/fixtures/${fixture}`], {
      env: {...process.env, PYTHONPATH: '.'}, encoding: 'utf8', timeout: 30000,
    });
    const report = JSON.parse(raw);
    assert.ok(report);
    assert.equal(report.external_calls ?? 0, 0);
  });
}
