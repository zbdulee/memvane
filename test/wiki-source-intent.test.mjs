import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('wiki source-intent planning is pure and enqueue_changed_sources keeps its queue bytes, consent, cap and deferrals',()=>{
 // Importing config fixes HOME-derived constants; keep them disposable.
 const home=mkdtempSync(join(process.cwd(),'qmd-wiki-source-intent-home-'));
 try{
  const p=spawnSync('python3',['test/fixtures/wiki-source-intent-checks.py'],{encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,QMD_RECALL_LOG:'',QMD_SETUP_GUARD_FIXTURE:'',QMD_SKIP_BACKGROUND_WORKER:'1',QMD_SANDBOX:'',QMD_SYNC_COMPILE_MAX:'',QMD_ENGINE:'',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
 }finally{removeTemp(home);}
});
