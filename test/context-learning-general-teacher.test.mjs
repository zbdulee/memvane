import {test} from 'node:test';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';

test('general project gold protects egress and durable dispatch boundaries',()=>{
 const out=execFileSync('python3',['test/fixtures/context-learning-general-teacher-checks.py'],{
   encoding:'utf8',timeout:30000,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
 assert.equal(out.trim(),'');
});
