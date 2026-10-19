import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('install evidence decisions are pure and shared_install keeps intent → enqueue → ACK ordering',()=>{
 // shared_install imports modules with HOME-derived constants; never the real HOME.
 const home=mkdtempSync(join(process.cwd(),'qmd-install-evidence-home-'));
 try{
  const p=spawnSync('python3',['test/fixtures/install-evidence-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SETUP_GUARD_FIXTURE:'',QMD_SKIP_BACKGROUND_WORKER:'1',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
  assert.match(p.stderr,/Ran 18 tests?/,p.stderr);
 }finally{removeTemp(home);}
});
