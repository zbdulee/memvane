import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('runtime execution approval is versioned, owner-private and policy-bound', () => {
  const raw = execFileSync('python3',
    ['test/fixtures/context-learning-runtime-authorization-checks.py'], {
      env: {...process.env, PYTHONPATH: 'core', QMD_RECALL_LOG: '',
        PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 10000,
    });
  const result = JSON.parse(raw);
  assert.equal(result.plan_bound, true);
  assert.equal(result.idempotent, true);
  assert.equal(result.stale_denied, true);
});
