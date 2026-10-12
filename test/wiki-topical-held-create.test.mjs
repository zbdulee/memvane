import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
for (const scenario of ['complement', 'full_duplicate', 'conflict']) {
  test(`K-42 ${scenario} holds one card while another publishes and source stays queued`, () => {
    const result = JSON.parse(execFileSync('python3', ['test/fixtures/wiki-topical-held-create.py'], {
      encoding: 'utf8', env: { ...process.env, QMD_HELD_CASE: scenario, QMD_RECALL_LOG: '' },
    }));
    assert.equal(result.case, scenario);
    assert.equal(result.k42Held, true);
    assert.equal(result.independentCardPublished, true);
    assert.equal(result.retrySafe, true);
    assert.equal(result.otherSourceClaimed, true);
    assert.equal(result.newRevisionRequeued, true);
  });
}
