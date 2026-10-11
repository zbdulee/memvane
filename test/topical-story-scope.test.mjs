import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('story-only topical opt-in resolves shared parent QMD route and pins policy', () => {
  const result = JSON.parse(execFileSync('python3',
    ['test/fixtures/topical-story-scope-checks.py'], {
      env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
      encoding: 'utf8', timeout: 15000}));
  assert.deepEqual(result, {exactStory:true,parentRoute:true,siblingDenied:true,
    settingsPin:true,privatePolicy:true});
});
