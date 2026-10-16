import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('actual recall stdin persists isolated project captures and gold queue without model calls', () => {
  const report = JSON.parse(execFileSync('python3', ['-B',
    'test/fixtures/context-learning-project-hook-e2e.py'], {
    cwd: process.cwd(), encoding: 'utf8', timeout: 300000,
    stdio: ['pipe','pipe','inherit'],
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  }));
  for (const key of ['disabledNoHistory', 'bothHostsShareProjectNamespace',
    'projectsRemainIsolated', 'durableGold', 'secretExcludedBeforeTransport',
    'insufficientDataDoesNotPromote', 'noModelImports', 'noGuardOverrides']) {
    assert.equal(report[key], true, key);
  }
  assert.equal(report.modelCalls, 0);
  assert.equal(report.externalCalls, 0);
});
