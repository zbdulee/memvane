import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
test('reviewed local marketplace adapters bind through real registry and code checks',()=>{
 const report=JSON.parse(execFileSync('python3',['test/fixtures/context-learning-local-adapter-binding-checks.py'],{encoding:'utf8',timeout:30000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}}));
 assert.equal(report.checks,20);assert.equal(report.modelCalls,0);assert.equal(report.providerCalls,0);
});
