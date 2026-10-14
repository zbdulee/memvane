import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
test('explicit installer bounds nested locks, preserves holders and resumes',()=>{
 const p=spawnSync('python3',['test/fixtures/install-lock-deadline-checks.py'],{
  encoding:'utf8',timeout:60000,
  env:{...process.env,QMD_SETUP_GUARD_FIXTURE:'',QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'},
 });
 assert.equal(p.status,0,p.stdout+p.stderr);
 assert.equal(Number(p.stderr.match(/Ran (\d+) tests/)?.[1]),9);
});
