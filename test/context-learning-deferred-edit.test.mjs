import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('deferred real edit keeps full snapshots and replays once after registration', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/context-learning-deferred-edit-checks.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: '',
      PYTHONDONTWRITEBYTECODE: '1'}}));
  assert.equal(result.immutablePrePost, true);
  assert.equal(result.ticketExtended, true);
  assert.equal(result.waitedForRegistration, true);
  assert.equal(result.oneReplay, true);
  assert.equal(result.providerCalls, 0);
});
