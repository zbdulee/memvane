import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
for (const [name,fixture,cases,timeout] of [
 ['scoped physical vector completeness and atomic metadata repair','vector-integrity-checks.py',13,10000],
 ['EPERM and delayed PID publication preserve other lock owners','worker-lock-checks.py',13,15000],
 ['abrupt installer exit resumes safely, including a surviving direct native child','install-abrupt-resume-checks.py',6,90000],
 ['normal worker repairs only defective vectors and audits its final repair','install-vector-resume-checks.py',3,60000],
]) {
 test(name,()=>{
  const p=spawnSync('python3',[`test/fixtures/${fixture}`],{encoding:'utf8',timeout,
   env:{...process.env,QMD_SETUP_GUARD_FIXTURE:'',QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
  assert.equal(p.status,0,p.stdout+p.stderr);
  assert.equal(Number(p.stderr.match(/Ran (\d+) tests/)?.[1]),cases);
  console.info(`${fixture}: ${cases} synthetic cases`);
 });
}
