import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('schema3 candidate read capability is bound to its verified checkout',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-reader-capability-')));
  try {
    const result=spawnSync('python3',['-B','test/fixtures/laya-reader-capabilities-checks.py'],{
      encoding:'utf8',timeout:120000,env:{...process.env,HOME:home,PYTHONDONTWRITEBYTECODE:'1'}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 11 tests\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});
