import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
for (const [name,group,cases,timeout] of [
 ['formal common install preserves v1, resumes failure and proves real hook routing','baseline',19,120000],
 ['source stability handles missed hooks, temporary edits and bounded retry','sources',4,60000],
]) test(name,()=>{
 const p=spawnSync('python3',['test/fixtures/shared-install-checks.py',group],{encoding:'utf8',timeout,env:{...process.env,QMD_SETUP_GUARD_FIXTURE:'',QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(p.status,0,p.stdout+p.stderr);
 assert.equal(Number(p.stderr.match(/Ran (\d+) tests/)?.[1]),cases);
 console.info(`synthetic install ${group} cases: ${cases}`);
});
