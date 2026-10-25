import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {performance} from 'node:perf_hooks';
// The fixture reports case name and monotonic milliseconds only. Parsing is
// bounded so a timed-out run still names the active case and cumulative cost.
const CASE=/shared-install-case (start|finish|interrupted) (test_[a-z0-9_]{1,120}|none)(?: (\d{1,9})ms)? total=(\d{1,9})ms/g;
const timing=(stderr,wrapper)=>{
 const text=String(stderr??'');let active=null,interrupted=null,seen=0;const done=[];
 for(const [,kind,name,ms,total] of text.matchAll(CASE)){
  if(++seen>200)break;
  if(kind==='start')active={name,at:Number(total)};
  else if(kind==='finish'){done.push({name,ms:Number(ms)});active=null;}
  else interrupted={name,ms:ms===undefined?'-':ms,total};
 }
 const suite=text.match(/shared-install-suite finish (\d{1,9})ms/)?.[1]??'unfinished';
 const slowest=[...done].sort((a,b)=>b.ms-a.ms).slice(0,5).map(c=>`${c.name}=${c.ms}ms`).join(', ')||'none';
 return [`shared-install timing: wrapper=${wrapper}ms python_suite=${suite}ms finished_cases=${done.length}`,
  interrupted?`interrupted: ${interrupted.name} ${interrupted.ms}ms total=${interrupted.total}ms`
   :active?`active without finish: ${active.name} started_at=${active.at}ms`:'active: none',
  `slowest: ${slowest}`].join('\n');
};
// Each unittest case runs in its own Python process with a strict per-case
// deadline (observed cases stay below 20s). There is deliberately no aggregate
// deadline here; the development guard bounds the whole file.
const CASE_TIMEOUT=25000,LIST_TIMEOUT=15000;
const NAME=/^test_[a-z0-9_]{1,120}$/;
const fixture=(args,timeout)=>spawnSync('python3',['test/fixtures/shared-install-checks.py',...args],
 {encoding:'utf8',timeout,env:{...process.env,QMD_SETUP_GUARD_FIXTURE:'',QMD_RECALL_LOG:'',PYTHONDONTWRITEBYTECODE:'1'}});
for (const [name,group,cases] of [
 ['formal common install preserves v1, resumes failure and proves real hook routing','baseline',20],
 ['source stability handles missed hooks, temporary edits and bounded retry','sources',4],
]) test(name,async t=>{
 const listed=fixture(['--list',group],LIST_TIMEOUT);
 assert.equal(listed.status,0,`shared-install ${group} listing failed signal=${listed.signal} error=${listed.error?.code??'none'}\n`+listed.stdout+listed.stderr);
 const names=listed.stdout.split('\n').filter(Boolean);
 assert.ok(names.every(n=>NAME.test(n)),`unexpected case listing:\n${listed.stdout}`);
 assert.equal(new Set(names).size,names.length,'duplicate case names');
 assert.equal(names.length,cases,`shared-install ${group} listed ${names.length} cases`);
 const results=[];
 for (const caseName of names) await t.test(caseName,()=>{
  const began=performance.now();
  const p=fixture(['--case',caseName],CASE_TIMEOUT);
  const wrapper=Math.round(performance.now()-began);
  const report=timing(p.stderr,wrapper);
  const ran=Number(p.stderr.match(/Ran (\d+) tests?\b/)?.[1]);
  results.push({caseName,wrapper,ok:p.status===0&&ran===1});
  const verdict=p.error?.code==='ETIMEDOUT'?`timeout after ${wrapper}ms (limit ${CASE_TIMEOUT}ms)`:`failure status=${p.status}`;
  assert.equal(p.status,0,`shared-install case ${verdict}: ${caseName}\n${report}\nsignal=${p.signal} error=${p.error?.code??'none'}\n`+p.stdout+p.stderr);
  assert.equal(ran,1,`shared-install case ${caseName} did not run exactly one test\n`+p.stderr);
 });
 const slowest=[...results].sort((a,b)=>b.wrapper-a.wrapper).slice(0,5).map(c=>`${c.caseName}=${c.wrapper}ms`).join(', ');
 console.info(`shared-install ${group} per-case wrappers: total=${results.reduce((s,c)=>s+c.wrapper,0)}ms slowest: ${slowest||'none'}`);
 assert.equal(results.filter(c=>c.ok).length,cases,`shared-install ${group} failed: `+results.filter(c=>!c.ok).map(c=>c.caseName).join(', '));
 console.info(`synthetic install ${group} cases: ${cases}`);
});
