import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('capture-bound view generations seal provenance and publish with epoch CAS',()=>{
 const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-view-gen-home-')));
 try {
  const r=spawnSync('python3',['-B','test/fixtures/laya-view-generations-checks.py'],{encoding:'utf8',timeout:240000,env:{...process.env,HOME:home,PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(r.status,0,r.status===null?JSON.stringify({signal:r.signal,error:r.error?.message}):r.stdout+r.stderr);
  assert.match(r.stderr,/Ran 23 tests/);
 } finally {removeTemp(home);}
});
