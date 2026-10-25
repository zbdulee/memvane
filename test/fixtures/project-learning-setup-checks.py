"""Owner-private disposable setup transactions; no model/teacher execution."""
import importlib.util
assert importlib.util.find_spec('project_learning_setup') is not None, 'project learning setup implementation missing'
import json,os,shutil,sqlite3,tempfile,time,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import project_learning_setup as setup
from context_learning import project_scope,learning_profile
from context_learning.laya_adapter import MODEL_FILES
from context_learning.store import canonical
from context_learning.shared import _project_key
from context_learning import adapter_binding
# Execute production imports from the copied installed Codex package, so guards
# validate an actual approved installation rather than a patched source path.
if 'QMD_SETUP_FIXTURE_PACKAGE' not in os.environ:
 with tempfile.TemporaryDirectory(prefix='qmd-setup-installed-',dir='/tmp') as outer:
  outer=Path(outer).resolve();home=outer/'home'
  package=home/'.codex/plugins/cache/qmd-auto-context-marketplace/qmd-auto-context/test'
  package.mkdir(parents=True)
  shutil.copytree(Path(setup.__file__).parent,package/'core',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copytree(Path(setup.__file__).parents[1]/'hooks',package/'hooks')
  result=subprocess.run([sys.executable,__file__],env={**os.environ,'PYTHONPATH':str(package/'core'),'QMD_SETUP_FIXTURE_PACKAGE':str(package)},capture_output=True,text=True)
  print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
  raise SystemExit(result.returncode)



def save(p,v):
 p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);p.write_text(canonical(v)+'\n');p.chmod(0o600)

def expect(reason,fn):
 try:fn()
 except ValueError as e:assert reason in str(e),(reason,str(e))
 else:raise AssertionError('accepted '+reason)

with tempfile.TemporaryDirectory(prefix='qmd-learning-setup-',dir='/tmp') as temp:
 base=Path(temp).resolve();home=Path(os.environ['QMD_SETUP_FIXTURE_PACKAGE']).parents[5]
 project=base/'project';project.mkdir(mode=0o700)
 settings={'indexing':True,'collections':['docs'],'collectionPaths':{'docs':'.'},'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':True}}
 save(project/'.auto-context/settings.json',settings)
 (project/'.auto-context').chmod(0o755)
 from context_learning.runtime_setup import default_managed_root
 root=default_managed_root(home).parent.parent/'live-selection'/_project_key(project);root.mkdir(mode=0o700,parents=True)
 model=base/'model';model.mkdir(mode=0o700)
 for name in MODEL_FILES:
  p=model/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
 save(model/'rl_agent_config.json',{'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
 python=base/'python';python.write_text('runtime');python.chmod(0o700)
 installs={}
 for host in ('claude','codex'):
  p=home/('.'+host)/'plugins/cache/qmd-auto-context-marketplace/qmd-auto-context/test';p.mkdir(parents=True,exist_ok=True)
  if host=='claude':shutil.copytree(Path(project_scope.__file__).parents[1],p/'core',ignore=shutil.ignore_patterns('__pycache__'))
  if host=='claude':shutil.copytree(Path(project_scope.__file__).parents[2]/'hooks',p/'hooks')
  save(p/('.'+host+'-plugin/plugin.json'),{'name':'qmd-auto-context','version':'test'})
  save(p/'package.json',{'name':'qmd-auto-context','version':'test'})
  installs[host]=[p]
 save(home/'.claude/plugins/known_marketplaces.json',{'qmd-auto-context-marketplace':{'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}})
 save(home/'.claude/plugins/installed_plugins.json',{'plugins':{'qmd-auto-context@qmd-auto-context-marketplace':[{'version':'test','installPath':str(installs['claude'][0])}]}})
 codex={'name':'qmd-auto-context','marketplaceName':'qmd-auto-context-marketplace','version':'test','installed':True,'enabled':True,'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}
 adapter=installs['codex'][0]/'core/context_learning/laya_adapter.py'
 save(root/'live-selector.json',{'schema':'qmd-live-selector-v1','trainerArgv':[str(python),str(adapter),str(model)],'timeoutSeconds':30,'maxRssMib':4096})
 artifact=base/'base.artifact';artifact.write_text('immutable base');artifact.chmod(0o600)
 save(root/'active-checkpoint.json',{'artifact_path':str(artifact),'artifact_sha256':setup.file_sha(artifact),'cycle_id':'base'})
 db=root/'shared-learning.sqlite3'
 with sqlite3.connect(db) as conn:conn.execute('CREATE TABLE captured (body TEXT)');conn.execute("INSERT INTO captured VALUES ('original capture')")
 db.chmod(0o600)
 for name in ('laya-inference-binding.json','laya-inference-manifest.json','laya-inference-proof.json'):save(root/name,{'original':name})
 original_settings=(project/'.auto-context/settings.json').read_bytes();original_pointer=(root/'active-checkpoint.json').read_bytes()
 expiry=int(time.time()*1000)+3600000;cutoff=int(time.time()*1000)
 with patch.object(Path,'home',return_value=home),patch('context_learning.history_activation._project_host_roots',return_value=installs),patch('context_learning.adapter_binding._codex_installed',return_value=codex):
  # Preparation must reject runtime identities that scoped activation refuses,
  # before writing a journal, policy, settings or namespace data.
  live_path=root/'live-selector.json';original_live=live_path.read_bytes()
  def reject_runtime(runtime):
   live=json.loads(original_live);live['trainerArgv'][0]=str(runtime);save(live_path,live)
   before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   try:
    expect('setup_runtime_invalid',lambda:setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff))
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert (project/'.auto-context/settings.json').read_bytes()==original_settings
   finally:live_path.write_bytes(original_live)
  foreign=Path('/usr/bin/true').resolve(strict=True)
  if foreign.stat().st_uid!=os.getuid():reject_runtime(foreign)
  alias=base/'runtime-hardlink';os.link(python,alias)
  try:reject_runtime(python)
  finally:alias.unlink()
  # A venv-style symlink to a safe owned target remains supported and bound.
  alias=base/'runtime-symlink';alias.symlink_to(python)
  live=json.loads(original_live);live['trainerArgv'][0]=str(alias);save(live_path,live)
  try:
   before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   linked=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
   assert linked['runtime']['path']==str(alias) and linked['runtime']['resolved']==str(python)
   assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  finally:live_path.write_bytes(original_live);alias.unlink()
  before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
  plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  assert plan['settings']['contextLearning']['capture'] is True and plan['settings']['contextLearning']['autoCycle'] is True
  assert plan['policy']['goldModel']=='gpt-6-luna' and plan['policy']['baseArtifact']==str(artifact)
  expect('acceptance',lambda:setup.activate(plan,accepted_sha256='0'*64))
  # Exact review CAS protects concurrent session/user configuration edits.
  save(project/'.auto-context/settings.json',{**settings,'topN':4})
  expect('changed',lambda:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan)))
  (project/'.auto-context/settings.json').write_bytes(original_settings)
  # Interrupted installation must keep hooks gated and leave recovery actionable.
  def fault(at):
   if at=='scope_written':raise RuntimeError('power loss')
  try:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan),fault=fault)
  except RuntimeError:pass
  else:raise AssertionError('fault did not run')
  assert json.loads((root/setup.NAME).read_text())['phase']=='installing'
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  result=setup.recover(project,root)
  assert result['status']=='recovered'
  assert not (root/'shared-policy.json').exists()
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  for boundary in ('prepared','intent','settings_written','profile_written'):
   plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
   def crash(at):
    if at==boundary:raise RuntimeError('interrupted '+boundary)
   try:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan),fault=crash)
   except RuntimeError:pass
   else:raise AssertionError('missing boundary '+boundary)
   if boundary=='prepared':save(root/'shared-policy.json',{'external':'concurrent creation'})
   setup.recover(project,root)
   assert (project/'.auto-context/settings.json').read_bytes()==original_settings
   if boundary=='prepared':
    assert json.loads((root/'shared-policy.json').read_text())=={'external':'concurrent creation'}
    (root/'shared-policy.json').unlink()
  plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  result=setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan))
  assert result['status']=='activated'
  cfg=json.loads((project/'.auto-context/settings.json').read_text())
  assert cfg['contextLearning']['capture'] and cfg['contextLearning']['autoCycle']
  assert cfg['sharedLearning']['enabled'] and cfg['sharedLearning']['autoApply']
  assert json.loads((root/setup.NAME).read_text())['phase']=='activated'
  policy=json.loads((root/'shared-policy.json').read_text())
  assert project_scope.require(root,policy,'local_train')['projectRoot']==str(project)
  assert learning_profile.require(root,policy,'apply')['autoApply']
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  target_settings=(project/'.auto-context/settings.json').read_bytes()
  save(project/'.auto-context/settings.json',{**cfg,'topN':3})
  expect('settings_changed',lambda:setup.recover(project,root))
  (project/'.auto-context/settings.json').write_bytes(target_settings)
  policy_bytes=(root/'shared-policy.json').read_bytes()
  save(root/'shared-policy.json',{**policy,'maxGoldCalls':22})
  expect('configuration_changed',lambda:setup.recover(project,root))
  (root/'shared-policy.json').write_bytes(policy_bytes)
  with sqlite3.connect(Path(result['backupRoot'])/'namespace/shared-learning.sqlite3') as conn:
   assert conn.execute('SELECT body FROM captured').fetchall()==[('original capture',)]
  # Recovery restores configuration only, preserving any subsequently captured data.
  db=root/'shared-learning.sqlite3'
  with sqlite3.connect(db) as conn:conn.execute("INSERT INTO captured VALUES ('new capture')")
  db.chmod(0o600)
  setup.recover(project,root)
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  with sqlite3.connect(db) as conn:assert conn.execute('SELECT body FROM captured').fetchall()==[('original capture',),('new capture',)]
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  # The release fixture's explicit local QA mode is reviewed as one exact
  # transaction. It cannot grant a teacher or apply a candidate.
  fixture=base/'qa';fixture.mkdir(mode=0o700)
  qa_project=fixture/'project';qa_project.mkdir(mode=0o700)
  qa_root=fixture/'learning-private';qa_root.mkdir(mode=0o700)
  save(fixture/'MANIFEST.json',{'schema':'qmd-release-qa-fixture-v1',
       'root':str(fixture),'fixtureId':'synthetic-qa','paths':{'project':str(qa_project)}})
  save(fixture/'.qa-fixture-owner',{'fixtureId':'synthetic-qa','uid':os.getuid()})
  save(qa_project/'.auto-context/settings.json',settings)
  from context_learning import local_cycle,runtime_setup,laya_adapter
  identity=setup.model_identity(model)
  # Runtime probes stay metadata-only stubs; the binding checks themselves are real.
  compatible={'status':'metadata_compatible','runtime_identity_sha256':'c'*64,'mps_available':True}
  probe_patch=patch.object(runtime_setup,'probe_runtime',return_value=compatible);probe_patch.start()
  qa_base=qa_root/('laya-base-'+identity+'.artifact')
  laya_adapter.prepare_base_artifact(model,qa_base)
  guard_state=base/'fake-guard-state';run=guard_state/'runs'/('a'*32);run.mkdir(mode=0o700,parents=True)
  parent_pid=os.getppid();calibration_pid=os.getpid()
  running={'run':str(run),'kind':'model-test','status':'running','settled':False,
       'completed':False,'resourceLimits':True,'proofNonce':'b'*48,'pid':parent_pid}
  save(run/'checkpoint.json',running)
  sample={'owned':[{'pid':parent_pid,'ppid':1},{'pid':calibration_pid,'ppid':parent_pid}],
          'tree_rss_bytes':1250*1024*1024,'elapsed_seconds':1}
  guard_patch=patch.object(setup,'_guard_state_root',return_value=guard_state);guard_patch.start()
  guard_env={'QMD_DEVELOPMENT_GUARD_RUN':str(run),'QMD_DEVELOPMENT_GUARD_NONCE':'b'*48}
  def qa_files():return {str(p):p.read_bytes() for p in fixture.rglob('*') if p.is_file()}
  # Without the reviewed binding journal, calibration and synthetic prepare
  # fail closed before any model call or setup write.
  unbound=qa_files()
  with patch.dict(os.environ,guard_env),patch.object(local_cycle,'LocalTrainer',side_effect=AssertionError('model started')):
   expect('synthetic_binding_required',lambda:setup.calibrate_synthetic(qa_project,qa_root))
  expect('synthetic_binding_required',lambda:setup.prepare(qa_project,qa_root,
       expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED))
  assert qa_files()==unbound
  # The fresh QA root is bound only by the official base and reviewed initializer.
  qa_binding=setup.plan_synthetic_bindings(qa_project,qa_root,runtime=python,adapter=adapter,
       model_dir=model,base_artifact=qa_base)
  assert setup.bind_synthetic(qa_binding,accepted_sha256=setup.proposal_sha256(qa_binding))['status']=='bound'
  # Unrelated namespace data arrives only after the binding is published.
  for name in ('laya-inference-binding.json','laya-inference-manifest.json','laya-inference-proof.json'):
   shutil.copyfile(root/name,qa_root/name);(qa_root/name).chmod(0o600)
  with sqlite3.connect(qa_root/'shared-learning.sqlite3') as conn:conn.execute('CREATE TABLE captured (body TEXT)')
  (qa_root/'shared-learning.sqlite3').chmod(0o600)
  def binding_rejects(call,*,fresh):
   # A missing, unfinished or altered journal or target fails closed and writes
   # nothing; fresh callers also reject a changed runtime dependency.
   journal=qa_root/setup.BINDINGS;selector=qa_root/'live-selector.json'
   saved={p:p.read_bytes() for p in (journal,selector)};clean=qa_files()
   def check(reason):
    try:expect(reason,call)
    finally:
     for p,raw in saved.items():p.write_bytes(raw);p.chmod(0o600)
    assert qa_files()==clean
   journal.unlink();check('synthetic_binding_required')
   row=json.loads(saved[journal]);row['phase']='intent';save(journal,row);check('binding_in_progress')
   row=json.loads(saved[journal]);row['plan']['files']['live-selector.json']['maxRssMib']=65536
   save(journal,row);check('synthetic_binding_changed')
   row['acceptedSha256']=setup.proposal_sha256(row['plan']);save(journal,row);check('synthetic_binding_changed')
   save(selector,{**json.loads(saved[selector]),'maxRssMib':65536});check('synthetic_binding_changed')
   if fresh:
    with patch.object(runtime_setup,'probe_runtime',return_value={**compatible,'runtime_identity_sha256':'d'*64}):
     check('synthetic_binding_inputs_changed')
  with patch.dict(os.environ,{'QMD_DEVELOPMENT_GUARD_RUN':'',
                              'QMD_DEVELOPMENT_GUARD_NONCE':''}):
   expect('guard_required',lambda:setup.calibrate_synthetic(qa_project,qa_root))
  with patch.dict(os.environ,guard_env),patch.object(local_cycle,'LocalTrainer',side_effect=AssertionError('model started')):
   binding_rejects(lambda:setup.calibrate_synthetic(qa_project,qa_root),fresh=True)
  assert not (qa_root/setup.CALIBRATION).exists()
  observed=[]
  class FakeTrainer:
   def __init__(self,argv,**kwargs):
    assert kwargs['max_rss_mib']==setup.SYNTHETIC_MAX_RSS_MIB==3584 and kwargs['require_monitor'] is True
   def call(self,mode,request,result_path,*,cwd):
    observed.append((mode,request))
    assert mode=='predict' and request['artifact_path']==str(qa_base)
    assert 'train' not in request and request['cases'][0].get('selected_ids') is None
    return ({'predictions':[{'request_id':'synthetic-calibration','selected_ids':['blue']}]},
            {'observed_peak_rss_kib':1000*1024,'resource_samples':3,'configured_cpu_threads':1})
  # Binding check, pre-call identity, post-call identity: only the last changes.
  with patch.dict(os.environ,guard_env),patch.object(local_cycle,'LocalTrainer',FakeTrainer),\
       patch.object(setup,'model_identity',side_effect=[identity,identity,'f'*64]):
   expect('inputs_changed',lambda:setup.calibrate_synthetic(qa_project,qa_root))
  assert not (qa_root/setup.CALIBRATION).exists()
  with patch.dict(os.environ,guard_env),patch.object(local_cycle,'LocalTrainer',FakeTrainer):
   assert setup.calibrate_synthetic(qa_project,qa_root)['status']=='calibration_pending_guard_settlement'
  assert len(observed)==2 and all(mode=='predict' for mode,_ in observed)
  evidence=json.loads((qa_root/setup.CALIBRATION).read_text())
  later={**sample,'elapsed_seconds':2}
  (run/'samples.jsonl').write_text(canonical(sample)+'\n'+canonical(later)+'\n');(run/'samples.jsonl').chmod(0o600)
  settled={**running,'status':'passed','settled':True,'completed':True}
  save(run/'checkpoint.json',settled)
  memory={'device':'mps','fixedMib':1875,'perPairMib':128,
          'estimateSource':'reviewed-forward-envelope:'+setup.proposal_sha256(evidence),
          'modelIdentitySha256':identity,'reserveFraction':.35,
          'maxVerifiedMicrobatchSize':1,'systemReserveMib':768,
          'reserveSource':'reviewed-synthetic-qa-floor'}
  before=(qa_project/'.auto-context/settings.json').read_bytes()
  expect('memory_profile_required',lambda:setup.prepare(qa_project,qa_root,
       expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED))
  forged={**evidence,'observedPeakRssKib':900*1024}
  expect('calibration_required',lambda:setup.prepare(qa_project,qa_root,
       expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED,
       memory_profile=memory,memory_evidence=forged))
  save(run/'checkpoint.json',running)
  expect('calibration_required',lambda:setup.prepare(qa_project,qa_root,
       expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED,
       memory_profile=memory,memory_evidence=evidence))
  save(run/'checkpoint.json',settled)
  expect('measured_profile_required',lambda:setup.prepare(qa_project,qa_root,
       expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED,
       memory_profile={**memory,'fixedMib':1512},memory_evidence=evidence))
  binding_rejects(lambda:setup.prepare(qa_project,qa_root,expires_at_unix_ms=expiry,
       not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED,
       memory_profile=memory,memory_evidence=evidence),fresh=True)
  qa_plan=setup.prepare(qa_project,qa_root,expires_at_unix_ms=expiry,
       not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED,
       memory_profile=memory,memory_evidence=evidence)
  assert qa_plan['bindingSha256']==setup.proposal_sha256(qa_binding)
  assert qa_plan['policy']['baseArtifact']==str(qa_base)
  assert qa_plan['teacherConsent'] is None
  assert qa_plan['policy']['maxRssMib']==3584 and qa_plan['policy']['batchMax']==100
  assert qa_plan['settings']['contextLearning']['capture'] is True
  assert qa_plan['settings']['contextLearning']['liveSelection'] is False
  assert qa_plan['settings']['sharedLearning']['autoApply'] is False
  expect('acceptance',lambda:setup.activate(qa_plan,accepted_sha256='0'*64))
  assert (qa_project/'.auto-context/settings.json').read_bytes()==before
  qa_result=setup.activate(qa_plan,accepted_sha256=setup.proposal_sha256(qa_plan))
  assert qa_result['status']=='activated' and qa_result['promotion']=='disabled'
  assert not (qa_root/'project-teacher-consent.json').exists()
  qa_policy=json.loads((qa_root/'shared-policy.json').read_text())
  # The frequent runtime path verifies the bound journal and targets without
  # probing the runtime or hashing the model, and fails closed on tampering.
  with patch.object(runtime_setup,'probe_runtime',side_effect=AssertionError('runtime probed')),\
       patch.object(setup,'model_identity',side_effect=AssertionError('model hashed')):
   assert setup.synthetic_local_only(qa_root,qa_project,qa_policy)
   binding_rejects(lambda:setup.synthetic_local_only(qa_root,qa_project,qa_policy),fresh=False)
   assert setup.synthetic_local_only(qa_root,qa_project,qa_policy)
  setup_journal=qa_root/setup.NAME
  setup_bytes=setup_journal.read_bytes();setup_journal.unlink()
  expect('synthetic_journal_required',lambda:setup.synthetic_local_only(qa_root,qa_project,qa_policy))
  setup_journal.write_bytes(setup_bytes);setup_journal.chmod(0o600)
  profile=learning_profile.require(qa_root,qa_policy,'local_train')
  assert profile['localTrain'] and not profile['autoApply']
  expect('profile_scope_changed_or_revoked',lambda:learning_profile.require(qa_root,qa_policy,'apply'))
  from context_learning import shared,project_worker,general_teacher
  qa_cfg=json.loads((qa_project/'.auto-context/settings.json').read_text())
  with patch.object(shared,'_safe_root',return_value=qa_root),patch.object(shared,'_policy',return_value={**qa_policy,'_runtime_authorization_v2':True}),\
       patch.object(general_teacher,'process_pending',side_effect=AssertionError('teacher called')),\
       patch.object(general_teacher,'read_weekly_quota',side_effect=AssertionError('quota called')),\
       patch.object(shared,'resource_report',return_value={'ready':True}),\
       patch.object(shared,'run_training_pending',return_value={'status':'waiting_fresh_batch'}):
   assert shared.launch(qa_cfg,qa_project)['status']=='manual_synthetic_training_required'
   worker=project_worker.run(qa_root,qa_project,qa_cfg)
   assert worker['teacher']['status']=='disabled_synthetic_local_only'
   assert worker['status']=='synthetic_training_start_required'
  assert setup.synthetic_budget_status(qa_root,qa_project,qa_policy)['status']=='synthetic_training_start_required'
  assert setup.start_synthetic_budget(qa_root,qa_project,qa_policy)['status']=='ready'
  assert setup.synthetic_budget_status(qa_root,qa_project,qa_policy)['status']=='ready'
  assert setup.start_synthetic_budget(qa_root,qa_project,qa_policy)['status']=='ready'
  from context_learning import automatic_staging
  deadline=json.loads((qa_root/setup.BUDGET).read_text())['deadlineUnixMs']/1000
  with patch.object(setup,'synthetic_budget_status',return_value={'status':'ready'}),\
       patch.object(automatic_staging.time,'time',return_value=deadline-.5),\
       patch.object(automatic_staging,'VerifiedLayaTrainer',side_effect=AssertionError('model started')):
   try:automatic_staging._trainer(qa_policy,None,root=qa_root)
   except automatic_staging.ResourceDeferred as exc:
    assert 'synthetic_training_budget_exhausted' in str(exc)
   else:raise AssertionError('synthetic deadline admitted a model')
  qa_settings=(qa_project/'.auto-context/settings.json').read_bytes()
  save(qa_project/'.auto-context/settings.json',{**json.loads(qa_settings),'topN':3})
  expect('settings_changed',lambda:setup.recover(qa_project,qa_root))
  (qa_project/'.auto-context/settings.json').write_bytes(qa_settings)
  assert setup.recover(qa_project,qa_root)['status']=='recovered'
  assert (qa_project/'.auto-context/settings.json').read_bytes()==before
  assert not (qa_root/'project-teacher-consent.json').exists()
  guard_patch.stop();probe_patch.stop()
  # A fresh synthetic root gets its runtime/model/base bindings only from the
  # reviewed initializer: exact fixture, installed adapter, prepared base.
  from context_learning import laya_adapter
  init=base/'qa-init';init.mkdir(mode=0o700)
  init_project=init/'project';init_project.mkdir(mode=0o700)
  init_root=init/'learning-private';init_root.mkdir(mode=0o700)
  other_root=init/'other-private';other_root.mkdir(mode=0o700)
  save(init/'MANIFEST.json',{'schema':'qmd-release-qa-fixture-v1',
       'root':str(init),'fixtureId':'synthetic-init','paths':{'project':str(init_project)}})
  save(init/'.qa-fixture-owner',{'fixtureId':'synthetic-init','uid':os.getuid()})
  save(init_project/'.auto-context/settings.json',settings)
  base_artifact=init_root/('laya-base-'+identity+'.artifact')
  laya_adapter.prepare_base_artifact(model,base_artifact)
  def snapshot():return {str(p):p.read_bytes() for p in init.rglob('*') if p.is_file()}
  inputs=dict(runtime=python,adapter=adapter,model_dir=model,base_artifact=base_artifact)
  compatible={'status':'metadata_compatible','runtime_identity_sha256':'c'*64,'mps_available':True}
  with patch.object(runtime_setup,'probe_runtime',return_value=compatible),\
       patch.object(local_cycle,'LocalTrainer',side_effect=AssertionError('model started')),\
       patch.object(local_cycle,'VerifiedLayaTrainer',side_effect=AssertionError('model started')),\
       patch.object(general_teacher,'process_pending',side_effect=AssertionError('teacher called')):
   clean=snapshot()
   expect('synthetic_fixture_invalid',lambda:setup.plan_synthetic_bindings(init_project,other_root,**inputs))
   owner=(init/'.qa-fixture-owner').read_bytes()
   save(init/'.qa-fixture-owner',{'fixtureId':'other-fixture','uid':os.getuid()})
   expect('synthetic_fixture_invalid',lambda:setup.plan_synthetic_bindings(init_project,init_root,**inputs))
   (init/'.qa-fixture-owner').write_bytes(owner)
   foreign_adapter=base/'foreign-adapter.py';shutil.copyfile(adapter,foreign_adapter)
   expect('adapter_invalid',lambda:setup.plan_synthetic_bindings(init_project,init_root,
        **{**inputs,'adapter':foreign_adapter}))
   with patch.object(runtime_setup,'probe_runtime',return_value={'status':'runtime_probe_failed'}):
    expect('runtime_invalid',lambda:setup.plan_synthetic_bindings(init_project,init_root,**inputs))
   expect('base_artifact_invalid',lambda:setup.plan_synthetic_bindings(init_project,init_root,
        **{**inputs,'base_artifact':artifact}))
   save(init_root/'shared-policy.json',{'unrelated':'state'})
   expect('root_not_fresh',lambda:setup.plan_synthetic_bindings(init_project,init_root,**inputs))
   (init_root/'shared-policy.json').unlink()
   assert snapshot()==clean
   init_plan=setup.plan_synthetic_bindings(init_project,init_root,**inputs)
   assert snapshot()==clean
   assert init_plan['files']['live-selector.json']['trainerArgv']==[str(python),str(adapter),str(model)]
   assert init_plan['files']['live-selector.json']['maxRssMib']==3584
   assert init_plan['files']['active-checkpoint.json']['artifact_path']==str(base_artifact)
   assert init_plan['model']['identitySha256']==identity
   digest=setup.proposal_sha256(init_plan)
   # Wrong or stale approval writes nothing, not even the lock file.
   expect('acceptance',lambda:setup.bind_synthetic(init_plan,accepted_sha256='0'*64))
   loosened={**init_plan,'files':{**init_plan['files'],'live-selector.json':
             {**init_plan['files']['live-selector.json'],'maxRssMib':65536}}}
   expect('reviewed_inputs_changed',lambda:setup.bind_synthetic(loosened,
        accepted_sha256=setup.proposal_sha256(loosened)))
   weights=model/'model.safetensors';original_weights=weights.read_bytes()
   weights.write_bytes(b'changed weights')
   # The base artifact is bound to the model identity, so changed weights no
   # longer match the prepared base and the accepted plan fails closed.
   expect('base_artifact_invalid',lambda:setup.bind_synthetic(init_plan,accepted_sha256=digest))
   weights.write_bytes(original_weights)
   base_bytes=base_artifact.read_bytes()
   base_artifact.write_text(canonical({'schema_version':1,'kind':'laya_base_head',
        'base_model_sha256':'f'*64,'adapter_version':laya_adapter.ADAPTER_VERSION})+'\n')
   expect('base_artifact_invalid',lambda:setup.bind_synthetic(init_plan,accepted_sha256=digest))
   base_artifact.write_bytes(base_bytes)
   assert snapshot()==clean and not (init_root/'project-learning-setup.lock').exists()
   # Interruption after the first publication resumes idempotently.
   def stop(at):
    if at=='published:live-selector.json':raise RuntimeError('power loss')
   try:setup.bind_synthetic(init_plan,accepted_sha256=digest,fault=stop)
   except RuntimeError:pass
   else:raise AssertionError('binding fault did not run')
   assert json.loads((init_root/setup.BINDINGS).read_text())['phase']=='intent'
   assert (init_root/'live-selector.json').exists() and not (init_root/'active-checkpoint.json').exists()
   expect('binding_in_progress',lambda:setup.plan_synthetic_bindings(init_project,init_root,**inputs))
   expect('binding_in_progress',lambda:setup.prepare(init_project,init_root,
        expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED))
   partial=(init_root/'live-selector.json').read_bytes()
   save(init_root/'live-selector.json',{**json.loads(partial),'maxRssMib':65536})
   expect('binding_changed',lambda:setup.bind_synthetic(init_plan,accepted_sha256=digest))
   assert not (init_root/'active-checkpoint.json').exists()
   (init_root/'live-selector.json').write_bytes(partial)
   # Resume removes only this transaction's exact journal-bound temps. A
   # prefix-matching foreign file (even with planned bytes) or a partial or
   # tampered bound temp is kept, and resume fails closed.
   planned={name:(canonical(v)+'\n').encode() for name,v in init_plan['files'].items()}
   stray=init_root/(setup.BINDING_TEMP+'foreign.tmp')
   stray.write_bytes(planned['active-checkpoint.json']);stray.chmod(0o600)
   expect('root_not_fresh',lambda:setup.bind_synthetic(init_plan,accepted_sha256=digest))
   assert stray.read_bytes()==planned['active-checkpoint.json'];stray.unlink()
   bound=setup._binding_temp(init_root,'active-checkpoint.json',digest)
   for body in (planned['active-checkpoint.json'][:-5],b'x'*len(planned['active-checkpoint.json'])):
    bound.write_bytes(body);bound.chmod(0o600)
    expect('binding_temp_unbound',lambda:setup.bind_synthetic(init_plan,accepted_sha256=digest))
    assert bound.read_bytes()==body and not (init_root/'active-checkpoint.json').exists()
   # Exact leftovers from a crash before link, and after link but before
   # unlink, are this transaction's own and are reclaimed on resume.
   bound.write_bytes(planned['active-checkpoint.json']);bound.chmod(0o600)
   os.link(init_root/'live-selector.json',setup._binding_temp(init_root,'live-selector.json',digest))
   assert setup.bind_synthetic(init_plan,accepted_sha256=digest)['status']=='bound'
   assert setup.bind_synthetic(init_plan,accepted_sha256=digest)['status']=='already_bound'
   for name,value in init_plan['files'].items():
    assert json.loads((init_root/name).read_text())==value
    assert (init_root/name).stat().st_mode&0o077==0
   assert {p.name for p in init_root.iterdir()}=={base_artifact.name,'live-selector.json',
        'active-checkpoint.json',setup.BINDINGS,'project-learning-setup.lock'}
   # No teacher consent, policy, profile, settings change or promotion.
   assert (init_project/'.auto-context/settings.json').read_bytes()==clean[str(init_project/'.auto-context/settings.json')]
   # The existing reviewed synthetic setup now consumes the bindings and still
   # requires its measured memory profile before any activation.
   expect('memory_profile_required',lambda:setup.prepare(init_project,init_root,
        expires_at_unix_ms=expiry,not_before_unix_ms=cutoff,mode=setup.SYNTHETIC_STAGED))
  # Symlink inputs are rejected before dereferencing them.
  p=root/'live-selector.json';raw=p.read_bytes();p.unlink();target=base/'other';target.write_bytes(raw);target.chmod(0o600);p.symlink_to(target)
  expect('unsafe',lambda:setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff))
print('project learning setup checks passed')
