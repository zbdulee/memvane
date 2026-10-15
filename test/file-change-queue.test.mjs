import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

for (const scenario of ['delete', 'rename', 'failed', 'outside', 'duplicate', 'recovery', 'lost-wakeup', 'first-snapshot', 'first-create', 'first-update', 'fifo', 'burst', 'same-stat', 'worker-failure', 'compile-cap', 'guards']) {
  test(`Claude deferred file-state detection: ${scenario} in isolated SQLite`, () => {
    const result = JSON.parse(execFileSync('python3', ['test/fixtures/file-change-queue.py', scenario],
      {encoding:'utf8', timeout:45000, env:{...process.env, QMD_RECALL_LOG:'', PYTHONDONTWRITEBYTECODE:'1'}}));
    assert.equal(result.pass, true);
    assert.equal(result.operatingAccess, false);
    assert.equal(result.externalModelCalls, 0);
  });
}
