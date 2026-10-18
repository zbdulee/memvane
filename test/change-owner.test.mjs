import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('stage B1/B2a routes a present edit, or an observed delete/move side, only to its approved owner and fails closed otherwise',()=>{
 // Import-time HOME constants must never point at the developer's real HOME.
 const home=mkdtempSync(join(process.cwd(),'qmd-change-owner-home-'));
 try{
  const p=spawnSync('python3',['test/change-owner-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SETUP_GUARD_FIXTURE:'',QMD_SKIP_BACKGROUND_WORKER:'1',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
 }finally{removeTemp(home);}
});
