import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('anchored technical selection binds nine of twelve legacy pages through publish replay', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/wiki-topical-anchor-selection.py'],
    {encoding: 'utf8', env: {...process.env, QMD_RECALL_LOG: ''}}));
  assert.equal(result.selectedLegacy, 9);
  assert.equal(result.census, 12);
  assert.equal(result.publishedReplay, true);
});
