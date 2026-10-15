import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('SessionStart opt-in notice resolves actual project settings, including a nested cwd', () => {
  const result=JSON.parse(execFileSync('python3',['test/fixtures/update-optin-notice.py'],
    {encoding:'utf8',timeout:15000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}}));
  assert.deepEqual(result,{configured:true,nested:true,pending:true,optout:true,externalCalls:0});
});
