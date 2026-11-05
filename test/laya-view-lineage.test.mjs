import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';
test('last-view snapshots classify lineage and verifier or pin failures stay bounded',()=>{
 const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-view-lineage-home-')));
 try {
  const selected=process.env.MEMVANE_TEST_METHOD;
  const r=spawnSync('python3',['-B','test/fixtures/laya-view-lineage-checks.py',...(selected?[`LineageChecks.${selected}`]:[])],{encoding:'utf8',timeout:240000,env:{...process.env,HOME:home,PYTHONDONTWRITEBYTECODE:'1'}});
  if (process.env.MEMVANE_TEST_GIT_TIMING && r.stdout) process.stdout.write(r.stdout);
  assert.equal(r.status,0,r.status===null?JSON.stringify({signal:r.signal,error:r.error?.message}):r.stdout+r.stderr);
  assert.match(r.stderr,selected?/Ran 1 test\b/:/Ran 6 tests/);
 } finally {removeTemp(home);}
});
