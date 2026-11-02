import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('synthetic view budgets measure ten Git views and preserve more than 2048 rows',()=>{
 const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-view-budget-home-')));
 try {
  const r=spawnSync('python3',['-B','test/fixtures/laya-view-budget-checks.py'],{
   encoding:'utf8',timeout:360000,
   env:{...process.env,HOME:home,PYTHONDONTWRITEBYTECODE:'1'},
  });
  assert.equal(r.status,0,r.status===null?JSON.stringify({signal:r.signal,error:r.error?.message}):r.stdout+r.stderr);
  assert.match(r.stderr,/Ran 2 tests/);
  const metadata=r.stdout.trim().split('\n').map(line=>JSON.parse(line));
  assert.deepEqual(metadata.map(item=>item.case),['ten_git_views','above_2048']);
  for(const item of metadata) console.log(JSON.stringify(item));
 } finally {removeTemp(home);}
});
