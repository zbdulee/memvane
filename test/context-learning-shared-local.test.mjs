import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('shared queue keeps project provenance and trains one reversible local trial', () => {
  const output = execFileSync('python3', ['test/fixtures/context-learning-shared-local-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8', timeout: 30000,
  });
  const report = JSON.parse(output);
  assert.equal(report.shared_queue, 'passed');
  assert.equal(report.local_trial, 'passed');
  assert.equal(report.external_calls, 0);
});

test('shared fact families keep independent holdouts and reject gold overlap', () => {
  const output = execFileSync('python3', ['test/fixtures/context-learning-shared-family-checks.py'], {
    env: {...process.env, PYTHONPATH: 'core', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8', timeout: 30000,
  });
  const report = JSON.parse(output);
  assert.equal(report.independent_holdout, true);
  assert.equal(report.no_false_quality, true);
});
