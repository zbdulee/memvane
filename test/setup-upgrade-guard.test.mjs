import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, unlinkSync, readdirSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {removeTemp, tempBase} from './helpers/temp.mjs';

function run(command, args, env, input='') {
  return spawnSync(command, args, {cwd: process.cwd(), env, input, encoding:'utf8', timeout:8000});
}
// Seeded-owner tests need a base under ~/.cache: tmpdir() resolves to
// /private/tmp, which _collection_owners refuses as a risky path.
function fixture({underCache=false}={}) {
  const root=underCache ? realpathSync(tempBase()) : tmpdir();
  const base=mkdtempSync(join(root,'qmd-upgrade-guard-'));
  const project=join(base,'project'); mkdirSync(project);
  const home=join(base,'home'); mkdirSync(home);
  const queue=join(base,'queue'); mkdirSync(queue,{mode:0o700});
  writeFileSync(join(project,'.auto-context.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  const docs=join(project,'docs');mkdirSync(docs);
  const called=join(base,'called');
  const fake=join(base,'fake.py');writeFileSync(fake,'import os\nfrom pathlib import Path\nPath(os.environ["CALLED"]).write_text("called")\n');
  const manager=join(base,'manager.sh');writeFileSync(manager,'#!/bin/sh\necho manager >> "$CALLED"\n',{mode:0o700});
  const env={...process.env, HOME:home, QMD_SETUP_GUARD_FIXTURE:'', QMD_RECALL_LOG:'',
    QMD_UPDATE_HOOK_QUEUE_DIR:queue, QMD_CORE_RECALL_SCRIPT:fake,
    QMD_CORE_POSTTOOL_SCRIPT:fake, QMD_CORE_INDEX_SCRIPT:fake,
    QMD_CORE_COMPILE_ENQUEUE_SCRIPT:fake, QMD_CORE_UPDATE_SCRIPT:manager,
    QMD_BACKEND_MANAGER:manager, CALLED:called};
  return {base,project,home,queue,docs,called,env};
}
function activateSharedStore(f) {
  const prepared=run('python3',['test/fixtures/prepare-shared-qmd-store.py',f.home],f.env);
  assert.equal(prepared.status,0,prepared.stderr);
}
// The synthetic store is a legacy (unscoped) selection, so check_project only
// admits an owner whose exact registry rows already exist. Seed those rows for
// the fixture project; no enrollment journal or grant is written.
function seedExistingOwnerRows(f) {
  const registry=join(f.home,'Library/Application Support/qmd-auto-context/shared-qmd/collections.json');
  writeFileSync(registry,JSON.stringify({docs:{scope:realpathSync(f.project),
    path:realpathSync(f.docs),indexed:true}}),{mode:0o600});
}

test('code upgrade blocks old project hooks before SessionStart for both hosts and preserves legacy bytes',()=>{
  const f=fixture();
  const legacy=readFileSync(join(f.project,'.auto-context.json'));
  for (const engine of ['codex','claude']) for (const action of ['recall','posttool','index','compile','gate','update']) {
    const result=run('bash',['hooks/run-hook',action,engine],f.env,
      JSON.stringify({cwd:f.project,prompt:'synthetic',hook_event_name:'UserPromptSubmit'}));
    assert.equal(result.status,0,`${engine}/${action}: ${result.stderr}`);
    assert.equal(result.stdout,'');
  }
  assert.equal(run('bash',['hooks/run-hook','topical-stop','codex'],
    {...f.env,QMD_TOPICAL_SANDBOX_ROOT:f.project},JSON.stringify({cwd:f.project})).stdout,'{}\n');
  assert.equal(existsSync(f.called),false);
  assert.deepEqual(readFileSync(join(f.project,'.auto-context.json')),legacy);
  assert.equal(existsSync(join(f.project,'.auto-context')),false);
  const manual=run('bash',['skills/query/scripts/query.sh',f.project,'synthetic'],f.env);
  assert.equal(manual.status,1,manual.stderr);
  assert.match(manual.stderr,/setup required/);
  assert.equal(existsSync(f.called),false);
});

test('first SessionStart only gives setup notice; old queued workers retain work',()=>{
  const f=fixture();
  const payload=JSON.stringify({cwd:f.project,hook_event_name:'SessionStart'});
  const first=run('bash',['hooks/run-hook','update-queued','codex'],f.env,payload);
  const second=run('bash',['hooks/run-hook','update-queued','claude'],f.env,payload);
  assert.match(first.stdout,/setup skill/);
  assert.equal(second.stdout,'');
  assert.equal(readdirSync(f.queue).filter(x=>x.endsWith('.job.json')).length,0);
  assert.equal(existsSync(f.called),false);
  const key=createHash('sha256').update(f.project).digest('hex').slice(0,32);
  const oldJob=join(f.queue,key+'.job.json');
  writeFileSync(oldJob,JSON.stringify({schema:'qmd-update-hook-job-v1',cwd:f.project}));
  const worker=run('python3',['core/update_hook_queue.py','worker',key],f.env);
  assert.equal(worker.status,0,worker.stderr);
  assert.equal(JSON.parse(readFileSync(join(f.queue,key+'.status.json'))).status,'setup_required');
  assert.equal(existsSync(oldJob),true);
  assert.equal(existsSync(f.called),false);
  const topical=join(f.project,'.topical-hook-jobs');mkdirSync(topical,{mode:0o700});
  writeFileSync(join(f.project,'.qmd-topical-sandbox'),'synthetic');
  const topicalJob=join(topical,'first.json');
  writeFileSync(topicalJob,JSON.stringify({schema:'topical-hook-job-v1',action:'reconcile',turnKey:'one'}));
  const topicalWorker=run('python3',['core/topical_hook_queue.py','worker',f.project],f.env);
  assert.equal(topicalWorker.status,0,topicalWorker.stderr);
  assert.equal(JSON.parse(readFileSync(join(f.project,'topical-hook-status.json'))).status,'setup_required');
  assert.equal(existsSync(topicalJob),true);
  const dirty=join(f.base,'dirty');writeFileSync(dirty,`docs\t${f.docs}\t${f.project}\n`);
  const qmd=join(f.base,'qmd.sh');writeFileSync(qmd,'#!/bin/sh\necho qmd >> "$CALLED"\n',{mode:0o700});
  const index=run('bash',['backend/index_worker.sh'],{...f.env,QMD_DIRTY_QUEUE:dirty,
    QMD_FAKE_QMD:qmd,QMD_CACHE_DIR:join(f.base,'cache'),QMD_LOCK_BASE:join(f.base,'locks'),QMD_NO_RELOAD:'1'});
  assert.equal(index.status,1,index.stderr);
  assert.equal(readFileSync(dirty,'utf8'),`docs\t${f.docs}\t${f.project}\n`);
  assert.equal(existsSync(f.called),false);
});

test('shared-store activation resumes new code; journal or optout blocks it',(t)=>{
  const f=fixture({underCache:true});
  t.after(()=>removeTemp(f.base));
  activateSharedStore(f);
  const auto=join(f.project,'.auto-context');mkdirSync(auto);
  writeFileSync(join(auto,'settings.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  seedExistingOwnerRows(f);
  unlinkSync(join(f.project,'.auto-context.json'));
  const pointer=join(auto,'qmd-index-active.json');
  writeFileSync(pointer,JSON.stringify({schema:'qmd-index-pointer-v1'}));
  const journal=join(auto,'install-update-journal.json');
  writeFileSync(journal,JSON.stringify({schema:'qmd-install-update-v1',phase:'activated'}));
  const ready=run('bash',['hooks/run-hook','recall','codex'],{...f.env,QMD_QUERY_FIXTURE:'synthetic'},
    JSON.stringify({cwd:f.project,prompt:'synthetic'}));
  assert.equal(ready.status,0,ready.stderr);
  assert.equal(existsSync(f.called),true);
  unlinkSync(f.called);
  writeFileSync(journal,JSON.stringify({schema:'qmd-install-update-v1',phase:'prepared'}));
  assert.equal(run('bash',['hooks/run-hook','recall','claude'],f.env,
    JSON.stringify({cwd:f.project,prompt:'synthetic'})).stdout,'');
  assert.equal(existsSync(f.called),false);
  writeFileSync(journal,JSON.stringify({schema:'qmd-install-update-v1',phase:'rolled_back'}));
  unlinkSync(pointer);
  // The plugin-wide store remains selected when a legacy local pointer is
  // retired; only the project's optout can disable its own hooks.
  assert.equal(run('python3',['core/setup_guard.py','check',f.project],f.env).status,0);
  writeFileSync(pointer,JSON.stringify({schema:'qmd-index-pointer-v1'}));
  writeFileSync(join(f.project,'.qmd-topical-v2-project'),'owner opt-in\n',{mode:0o600});
  const opt=run('python3',['-c',`import sys;sys.path.insert(0,'core');import config;config.write_local_optout(sys.argv[1])`,f.project],f.env);
  assert.equal(opt.status,0,opt.stderr);
  assert.equal(run('python3',['core/setup_guard.py','check',f.project],f.env).status,3);
  assert.equal(run('bash',['hooks/run-hook','recall','claude'],f.env,
    JSON.stringify({cwd:f.project,prompt:'synthetic'})).stdout,'');
  assert.equal(existsSync(f.called),false);
  assert.equal(run('bash',['hooks/run-hook','topical-stop','codex'],f.env,
    JSON.stringify({cwd:f.project,hook_event_name:'Stop'})).stdout,'{}\n');
  assert.equal(run('bash',['hooks/run-hook','topical-reconcile','claude'],f.env,
    JSON.stringify({cwd:f.project,hook_event_name:'SessionStart'})).stdout,'');
  assert.equal(existsSync(join(f.project,'.topical-hook-jobs')),false);
  assert.equal(existsSync(join(f.project,'topical-reconcile-state.json')),false);
  const optoutNotice=run('bash',['hooks/run-hook','update-queued','codex'],f.env,
    JSON.stringify({cwd:f.project,hook_event_name:'SessionStart'}));
  assert.equal(optoutNotice.stdout,'');
});

test('topical hook binds selected opted-in project from host cwd without test root',(t)=>{
  const f=fixture({underCache:true});
  t.after(()=>removeTemp(f.base));
  activateSharedStore(f);
  const auto=join(f.project,'.auto-context');mkdirSync(auto);
  writeFileSync(join(auto,'settings.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  seedExistingOwnerRows(f);
  unlinkSync(join(f.project,'.auto-context.json'));
  writeFileSync(join(auto,'qmd-index-active.json'),JSON.stringify({schema:'qmd-index-pointer-v1'}));
  const marker=join(f.project,'.qmd-topical-v2-project');
  writeFileSync(marker,'owner opt-in\n',{mode:0o600});
  const env={...f.env};
  for (const key of ['QMD_TOPICAL_PROJECT_ROOT','QMD_TOPICAL_SANDBOX_ROOT','QMD_SETUP_GUARD_FIXTURE']) delete env[key];
  const payload=join(f.base,'payload.json');
  writeFileSync(payload,JSON.stringify({cwd:f.project,hook_event_name:'Stop'}));
  for (const engine of ['codex','claude']) {
    const resolved=run('python3',['core/setup_guard.py','hook-root','topical-stop',payload,f.base],env);
    assert.equal(resolved.status,0,`${engine}: ${resolved.stderr}`);
    assert.equal(resolved.stdout.trim(),realpathSync(f.project));
  }
  const opt=run('python3',['-c',`import sys;sys.path.insert(0,'core');import config;config.write_local_optout(sys.argv[1])`,f.project],env);
  assert.equal(opt.status,0,opt.stderr);
  assert.equal(run('python3',['core/setup_guard.py','hook-root','topical-stop',payload,f.base],env).status,3);
});

test('version change gives one local setup review notice',()=>{
  const f=fixture();
  activateSharedStore(f);
  const auto=join(f.project,'.auto-context');mkdirSync(auto);
  writeFileSync(join(auto,'settings.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  unlinkSync(join(f.project,'.auto-context.json'));
  writeFileSync(join(auto,'qmd-index-active.json'),JSON.stringify({schema:'qmd-index-pointer-v1'}));
  writeFileSync(join(auto,'install-update-journal.json'),JSON.stringify({schema:'qmd-install-update-v1',phase:'activated'}));
  const canonical=realpathSync(f.project);
  const key=createHash('sha256').update(canonical).digest('hex').slice(0,32);
  writeFileSync(join(f.queue,`setup-version-${key}.json`),JSON.stringify({schema:'qmd-setup-version-v1',project:canonical,version:'0.0.0'}));
  const script='import sys;sys.path.insert(0,"core");import update_hook_queue as q;from pathlib import Path;q._setup_notice(sys.argv[1],Path(sys.argv[2]))';
  const first=run('python3',['-c',script,f.project,f.queue],f.env);
  const second=run('python3',['-c',script,f.project,f.queue],f.env);
  assert.equal(first.status,0,first.stderr);
  assert.match(first.stdout,/plugin_version_changed/);
  assert.equal(second.stdout,'');
  assert.equal(existsSync(f.called),false);
});

test('unreadable legacy settings are setup-required before any hook work',()=>{
  const f=fixture();
  writeFileSync(join(f.project,'.auto-context.json'),'{invalid-json');
  const state=run('python3',['core/setup_guard.py','check',f.project],f.env);
  assert.equal(state.status,3,state.stderr);
  const hook=run('bash',['hooks/run-hook','recall','codex'],f.env,
    JSON.stringify({cwd:f.project,prompt:'synthetic'}));
  assert.equal(hook.status,0,hook.stderr);
  assert.equal(hook.stdout,'');
  assert.equal(existsSync(f.called),false);
});

test('prepared journal gives one SessionStart repair notice while work stays blocked',(t)=>{
  const f=fixture({underCache:true});
  t.after(()=>removeTemp(f.base));
  activateSharedStore(f);
  const auto=join(f.project,'.auto-context');mkdirSync(auto);
  writeFileSync(join(auto,'settings.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  seedExistingOwnerRows(f);
  unlinkSync(join(f.project,'.auto-context.json'));
  writeFileSync(join(auto,'qmd-index-active.json'),JSON.stringify({schema:'qmd-index-pointer-v1'}));
  const journal=join(auto,'install-update-journal.json');
  writeFileSync(journal,JSON.stringify({schema:'qmd-install-update-v1',phase:'prepared'}));
  const payload=JSON.stringify({cwd:f.project,hook_event_name:'SessionStart'});
  const first=run('bash',['hooks/run-hook','update-queued','codex'],f.env,payload);
  const second=run('bash',['hooks/run-hook','update-queued','claude'],f.env,payload);
  assert.equal(first.status,0,first.stderr);
  assert.match(first.stdout,/setup_transition_pending/);
  assert.equal(second.stdout,'');
  const markers=readdirSync(f.queue).filter(x=>x.startsWith('setup-notice-') && x.endsWith('.json'));
  assert.equal(markers.length,1);
  assert.equal(JSON.parse(readFileSync(join(f.queue,markers[0]))).reason,'setup_transition_pending');
  assert.equal(readdirSync(f.queue).filter(x=>x.endsWith('.job.json')).length,0);
  assert.equal(JSON.parse(readFileSync(journal)).phase,'prepared');
  assert.equal(existsSync(f.called),false);
});

test('invalid index pointer gives one SessionStart repair notice without touching it',()=>{
  const f=fixture();
  const auto=join(f.project,'.auto-context');mkdirSync(auto);
  writeFileSync(join(auto,'settings.json'),JSON.stringify({collections:['docs'],collectionPaths:{docs:'docs'}}));
  unlinkSync(join(f.project,'.auto-context.json'));
  const pointer=join(auto,'qmd-index-active.json');
  writeFileSync(pointer,'{invalid-json');
  const before=readFileSync(pointer);
  const payload=JSON.stringify({cwd:f.project,hook_event_name:'SessionStart'});
  const first=run('bash',['hooks/run-hook','update-queued','claude'],f.env,payload);
  const second=run('bash',['hooks/run-hook','update-queued','codex'],f.env,payload);
  assert.equal(first.status,0,first.stderr);
  assert.match(first.stdout,/invalid_project_pointer/);
  assert.equal(second.stdout,'');
  const markers=readdirSync(f.queue).filter(x=>x.startsWith('setup-notice-') && x.endsWith('.json'));
  assert.equal(markers.length,1);
  assert.equal(JSON.parse(readFileSync(join(f.queue,markers[0]))).reason,'invalid_project_pointer');
  assert.equal(readdirSync(f.queue).filter(x=>x.endsWith('.job.json')).length,0);
  assert.deepEqual(readFileSync(pointer),before);
  assert.equal(existsSync(f.called),false);
});
