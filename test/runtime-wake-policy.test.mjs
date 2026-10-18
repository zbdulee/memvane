import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('runtime wake policy keeps validation, priority, retry backoff, busy lease and revision ACK semantics',()=>{
 // Import-time HOME constants must never point at the developer's real HOME.
 const home=mkdtempSync(join(process.cwd(),'qmd-runtime-wake-policy-home-'));
 try{
  const p=spawnSync('python3',['test/fixtures/runtime-wake-policy-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SETUP_GUARD_FIXTURE:'',QMD_SANDBOX:'',QMD_RUNTIME_DISPATCH:'',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
 }finally{removeTemp(home);}
});
