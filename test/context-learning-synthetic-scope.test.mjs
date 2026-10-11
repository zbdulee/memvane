import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('only the exact marked synthetic project is eligible for Codex gold', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-synthetic-scope-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 10000,
    });
  const report = JSON.parse(raw);
  assert.equal(report.synthetic_allowed, true);
  assert.equal(report.ordinary_denied, true);
  assert.equal(report.tamper_denied, true);
  assert.equal(report.external_calls, 0);
});
