import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('v2 recovery survives owner death while readers retain the endpoint',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-v2-crash-')));
  try {
    const result=spawnSync('python3',['-B','test/fixtures/laya-v2-crash-checks.py'],{
      encoding:'utf8',timeout:240000,env:{...process.env,HOME:home,TMPDIR:home,
        QMD_SETUP_GUARD_FIXTURE:'1',PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    if(result.stdout)process.stdout.write(result.stdout);
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('v1 restore survives owner death and resumes the sealed endpoint',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-restore-crash-')));
  try {
    const result=spawnSync('python3',['-B','test/fixtures/laya-v2-crash-checks.py'],{
      encoding:'utf8',timeout:240000,env:{...process.env,HOME:home,TMPDIR:home,
        MEMVANE_TEST_CRASH_KIND:'restore',QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    if(result.stdout)process.stdout.write(result.stdout);
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});
