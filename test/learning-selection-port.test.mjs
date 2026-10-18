import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('recall live-selection candidate port is read-only and matches the pre-extraction inline behavior',()=>{
 // Import-time HOME constants must never point at the developer's real HOME.
 const home=mkdtempSync(join(process.cwd(),'qmd-selection-port-home-'));
 try{
  const p=spawnSync('python3',['test/fixtures/learning-selection-port-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SKIP_BACKGROUND_WORKER:'1',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
 }finally{removeTemp(home);}
});
