import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('candidate APIs are reachable only through the read-only adapter',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-candidate-isolation-')));
  try {
    const result=spawnSync('python3',['-B','test/fixtures/laya-checkout-view-checks.py',
      'NoActivation.test_api_writes_nothing_and_no_live_route_uses_it'],{
      encoding:'utf8',timeout:60000,env:{...process.env,HOME:home,PYTHONDONTWRITEBYTECODE:'1'}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});
