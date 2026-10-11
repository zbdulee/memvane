import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('one source update atomically refreshes two old cards and adds one new card', () => {
  const raw = execFileSync('python3', ['test/fixtures/wiki-topical-multicard-refresh.py'], {
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
    encoding: 'utf8', timeout: 30000,
  });
  const report = JSON.parse(raw);
  assert.equal(report.multicardRefresh, true);
  assert.equal(report.oldCardsRetired, 2);
  assert.equal(report.newCardsPublished, 3);
  assert.equal(report.generationCalls, 1);
  assert.equal(report.verificationCalls, 3);
  assert.equal(report.queueSettled, true);
  assert.equal(report.crossSourceRetirementBlocked, true);
  assert.equal(report.journalTamperBlocked, true);
  assert.equal(report.retirementHardlinkReplay, true);
  assert.equal(report.partialSourceDeletionRecovered, true);
});
