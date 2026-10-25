import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('Laya UID wake routes into its exact sealed target and retains failed work',()=>{
  const home=mkdtempSync(join(process.cwd(),'qmd-laya-runtime-dispatch-home-'));
  try{
    const result=spawnSync('python3',['test/fixtures/laya-runtime-dispatch-checks.py'],{
      encoding:'utf8',timeout:120000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',
        QMD_SETUP_GUARD_FIXTURE:'',QMD_SANDBOX:'',PYTHONDONTWRITEBYTECODE:'1'}});
    assert.equal(result.status,0,result.stdout+result.stderr);
  }finally{removeTemp(home);}
});
