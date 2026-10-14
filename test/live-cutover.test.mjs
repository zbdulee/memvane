import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('live sessions retain legacy writes while scoped shared activation fences real writers',()=>{
 const p=spawnSync('python3',['test/fixtures/live-cutover-checks.py'],{encoding:'utf8',timeout:60000,
  env:{...process.env,QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(p.status,0,p.stdout+p.stderr);
});
