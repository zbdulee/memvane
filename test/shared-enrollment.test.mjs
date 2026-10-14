import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('explicit shared enrollment preserves existing scope and closes on collision or inspection denial',()=>{
 const p=spawnSync('python3',['test/fixtures/shared-enrollment-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(p.status,0,p.stdout+p.stderr);
});
