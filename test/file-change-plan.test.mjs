import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('file-change planning is pure and its effects run index, wiki, deferral rollback, persist, then ACK',()=>{
 // Importing sync loads config; its import-time HOME constants must stay disposable.
 const home=mkdtempSync(join(process.cwd(),'qmd-file-change-plan-home-'));
 try{
  const p=spawnSync('python3',['test/fixtures/file-change-plan-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SETUP_GUARD_FIXTURE:'',QMD_SKIP_BACKGROUND_WORKER:'1',QMD_SANDBOX:'',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
 }finally{removeTemp(home);}
});
