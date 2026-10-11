import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('shared QMD deletion retires only an owned, physically removed collection', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/plugin-shared-prune-checks.py'], {
      encoding: 'utf8', timeout: 10000,
      env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: ''},
    }));
  assert.equal(result.exactOwner, true);
  assert.equal(result.waitedForPhysicalRemoval, true);
  assert.equal(result.retired, 1);
});
