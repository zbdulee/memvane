import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('scoped Stop coalesces synthetic CUD through fake Codex, QMD and learning proof', () => {
  const result=JSON.parse(execFileSync('python3',
    ['test/fixtures/topical-story-auto-e2e.py'],{
      env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'},
      encoding:'utf8',timeout:30000}));
  assert.deepEqual(result,{create:true,update:true,delete:true,sessionRecovery:true,
    fakeCodexCalls:6,externalProviderCalls:0,indexedAndRegistered:true});
});
