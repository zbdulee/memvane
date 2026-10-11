import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('scoped direct QMD writer uses shared non-stealing lock',()=>{
  const result=JSON.parse(execFileSync('python3',['test/fixtures/qmd-writer-lock-checks.py'],{
    env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'},
    encoding:'utf8',timeout:5000}));
  assert.deepEqual(result,{busyDenied:true,released:true,deadOwnerReclaimed:true});
});
