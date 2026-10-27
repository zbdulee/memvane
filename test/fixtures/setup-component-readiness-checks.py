"""Synthetic-only preparation status checks. No ML, downloads or live hosts."""
import hashlib,importlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
import setup_entry,qmd_runtime,update_hook_queue,qmd_shared_store as S,shared_qmd_setup as B
from context_learning import runtime_setup as R
from context_learning.laya_adapter import MODEL_FILES,model_identity

class Readiness(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.home=Path(self.tmp.name).resolve();self.root=R.default_managed_root(self.home)
  self.project=self.home/'project';self.project.mkdir();(self.project/'.auto-context').mkdir()
  self.settings=self.project/'.auto-context/settings.json';self.settings.write_text(json.dumps({'indexing':True,'collections':['docs'],'collectionPaths':{'docs':'docs'},'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':False},'sharedLearning':{'enabled':False,'trialSelection':False}}))
  self.env=patch.dict(os.environ,{'HOME':str(self.home),'QMD_RECALL_LOG':'','QMD_SUPPRESS_NOTICE':''});self.env.start();self.addCleanup(self.env.stop)
  self.found={'inventory':{'status':'ready_to_plan','project':str(self.project),'config':{'kind':'modern'},'managedQmd':{'status':'selected_managed','proof':{'version':qmd_runtime.VERSION,'capabilities':qmd_runtime.CAPABILITIES}},'managedLaya':None,'legacyWiki':[]},'discovered':{'sharedStoreStatus':'selected','sharedStoreReason':None,'routeSelected':True,'commonStoreSelected':True}}
  self.qmd_generation=self.home/'qmd-generation';self.qmd_generation.mkdir(mode=0o700)
  self.embedding=self.home/'embed.gguf';self.embedding.write_bytes(b'GGUFsynthetic embedding');self.embedding.chmod(0o600)
  proof=self.qmd_generation/'bootstrap-proof.json';proof.write_text(json.dumps({'schema':B.SCHEMA,'modelPath':str(self.embedding),'modelSha256':hashlib.sha256(self.embedding.read_bytes()).hexdigest()}));proof.chmod(0o600)
  self.qmd_select=patch.object(S,'select',return_value={'generation':str(self.qmd_generation)});self.qmd_select.start();self.addCleanup(self.qmd_select.stop)
  self.discovery=patch.object(setup_entry,'discover',return_value=self.found);self.discovery.start();self.addCleanup(self.discovery.stop)
  self.version=R.LAYA_PIN
  self.probe=patch.object(R,'_run_probe',side_effect=lambda *_a,**_k:{'python':R.PYTHON_PIN,'versions':dict(R.MANAGED_DEPENDENCIES,laya=self.version),'laya_import':self.version,'torch_import':R.MANAGED_DEPENDENCIES['torch'],'mps_available':False,'laya_direct_url':None})
  self.probe_mock=self.probe.start();self.addCleanup(self.probe.stop)
 def prepare(self):
  self.exe=self.root/'generations/one/bin/python';self.exe.parent.mkdir(parents=True);self.exe.write_bytes(b'#!/bin/sh\nexit 0\n');self.exe.chmod(0o700);self.root.chmod(0o700)
  self.model=self.home/'model';self.model.mkdir()
  for name in MODEL_FILES:
   p=self.model/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
  (self.model/'rl_agent_config.json').write_text(json.dumps({'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256}))
  (self.model/'encoder/config.json').write_text('{"max_position_embeddings":8192}')
  (self.model/'tokenizer/tokenizer_config.json').write_text('{"model_max_length":8192}')
  self.adapter=Path('core/context_learning/laya_adapter.py').resolve()
  self.attest={'schema_version':1,'runtime_identity_sha256':R.probe_runtime(self.exe,managed_root=self.root)['runtime_identity_sha256'],'base_model_sha256':model_identity(self.model),'adapter_sha256':hashlib.sha256(self.adapter.read_bytes()).hexdigest(),'data_kind':'synthetic_compact_wiki','checks':dict.fromkeys(R.CHECKS,True)}
  self.pointer=self.root/'active.json';self.pointer.write_text(json.dumps({'executable':str(self.exe),'runtime_identity_sha256':self.attest['runtime_identity_sha256'],'attestation':self.attest,'adapter_path':str(self.adapter),'model_dir':str(self.model)}));self.pointer.chmod(0o600)
 def test_missing_required_laya_not_up_to_date_even_when_learning_disabled(self):
  before=self.settings.read_bytes();row=setup_entry.check_update(self.project)
  self.assertEqual(row['status'],'update_review_required',row)
  self.assertIn('laya_managed_runtime_not_prepared',row['reasons'])
  self.assertTrue(row['qmdReady']);self.assertFalse(row['layaReady']);self.assertFalse(row['overallReady'])
  self.assertEqual(self.settings.read_bytes(),before);self.assertFalse(self.root.exists())
 def test_full_attested_local_multilingual_runtime_is_overall_ready(self):
  self.prepare();row=setup_entry.check_update(self.project)
  self.assertEqual(row['status'],'up_to_date',row);self.assertTrue(row['overallReady'])
  self.assertEqual(row['componentReadiness']['laya']['status'],'laya_ready')
  self.assertEqual(row['targetLayaVersion'],R.LAYA_PIN)
 def test_missing_qmd_embedding_model_prevents_overall_ready(self):
  self.prepare();self.embedding.unlink();row=setup_entry.check_update(self.project)
  self.assertFalse(row['qmdReady']);self.assertTrue(row['layaReady']);self.assertFalse(row['overallReady']);self.assertIn('qmd_local_embedding_model_required',row['reasons'])
 def test_changed_qmd_embedding_model_prevents_overall_ready(self):
  self.prepare();self.embedding.write_bytes(b'GGUFsynthetic drift');row=setup_entry.check_update(self.project)
  self.assertFalse(row['overallReady']);self.assertIn('qmd_embedding_model_changed_requires_review',row['reasons'])
 def test_wrong_runtime_version_requires_review(self):
  self.prepare();self.version='0.3.19';row=setup_entry.check_update(self.project)
  self.assertEqual(row['status'],'update_review_required');self.assertFalse(row['layaReady'])
 def test_changed_weights_require_review(self):
  self.prepare();(self.model/'model.safetensors').write_text('synthetic drift')
  row=setup_entry.check_update(self.project);self.assertIn('laya_managed_source_changed',row['reasons'])
 def test_missing_weights_require_review(self):
  self.prepare();(self.model/'model.safetensors').unlink()
  row=setup_entry.check_update(self.project);self.assertFalse(row['layaReady'])
 def test_attestation_of_different_adapter_is_not_current(self):
  self.prepare();old=self.home/'old-adapter.py';old.write_text('# preserved old adapter\n')
  value=json.loads(self.pointer.read_text());value['adapter_path']=str(old);value['attestation']['adapter_sha256']=hashlib.sha256(old.read_bytes()).hexdigest();self.pointer.write_text(json.dumps(value))
  row=setup_entry.check_update(self.project);self.assertIn('laya_current_adapter_required',row['reasons'])
 def test_attested_english_model_is_not_v2_edit_model_ready(self):
  self.prepare();(self.model/'rl_agent_config.json').write_text('{"encoder":"answerdotai/ModernBERT-large","max_len":512,"head_max_len":192}')
  value=json.loads(self.pointer.read_text());value['attestation']['base_model_sha256']=model_identity(self.model);self.pointer.write_text(json.dumps(value))
  row=setup_entry.check_update(self.project);self.assertIn('laya_edit_model_required',row['reasons'])
 def test_invalid_pointer_is_reported_without_traceback_or_repair(self):
  self.prepare();self.pointer.write_text('{bad');before=self.pointer.read_bytes()
  row=setup_entry.check_update(self.project);self.assertFalse(row['overallReady']);self.assertIn('laya_invalid_managed_pointer',row['reasons']);self.assertEqual(self.pointer.read_bytes(),before)
 def test_pointer_publication_during_check_cannot_inherit_old_proof(self):
  self.prepare();original=R.choose_runtime
  def changed(*a,**k):
   result=original(*a,**k);value=json.loads(self.pointer.read_text());value['runtime_identity_sha256']='f'*64;self.pointer.write_text(json.dumps(value));return result
  with patch.object(R,'choose_runtime',side_effect=changed):row=setup_entry.check_update(self.project)
  self.assertFalse(row['overallReady']);self.assertIn('laya_preparation_changed_during_check',row['reasons'])
 def test_qmd_not_ready_keeps_laya_ready_separate(self):
  self.prepare();self.found['discovered']['routeSelected']=False
  row=setup_entry.check_update(self.project);self.assertFalse(row['qmdReady']);self.assertTrue(row['layaReady']);self.assertFalse(row['overallReady'])
 def test_cheap_notice_preflight_does_not_probe_or_hash_weights(self):
  self.prepare();module=importlib.import_module('setup_readiness')
  with patch.object(R,'_run_probe',side_effect=AssertionError('unexpected runtime execution')),patch('context_learning.laya_adapter.model_identity',side_effect=AssertionError('unexpected weight hashing')):
   row=module.laya_preflight();self.assertIsNone(row['reason']);self.assertFalse(row['ready']);self.assertEqual(row['verification'],'preflight_only')
 def test_missing_laya_has_single_local_notice_without_blocking_qmd(self):
  import contextlib,io
  queue=self.home/'queue';queue.mkdir(mode=0o700)
  with patch('qmd_route.project_paths',return_value={'shared':True}):
   out=io.StringIO()
   with contextlib.redirect_stdout(out):
    update_hook_queue._setup_notice(self.project,queue,{'status':'ready'})
    update_hook_queue._setup_notice(self.project,queue,{'status':'ready'})
  self.assertEqual(out.getvalue().count('setup skill'),1);self.assertIn('laya_managed_runtime_not_prepared',out.getvalue())
  self.assertFalse(self.root.exists());self.probe_mock.assert_not_called()
 def stale_adapter(self):
  old=self.home/'old-adapter.py';old.write_text('# preserved old adapter\n')
  value=json.loads(self.pointer.read_text());value['adapter_path']=str(old);value['attestation']['adapter_sha256']=hashlib.sha256(old.read_bytes()).hexdigest();self.pointer.write_text(json.dumps(value))
 def generation(self):
  gen=self.root/'generations/g-one';self.exe=gen/'venv/bin/python';self.exe.parent.mkdir(parents=True);self.exe.write_bytes(b'#!/bin/sh\nexit 0\n');self.exe.chmod(0o700)
  for d in (self.root,self.root/'generations',gen):d.chmod(0o700)
  identity=R.probe_runtime(self.exe,managed_root=self.root)['runtime_identity_sha256']
  prepared=gen/'prepared.json';prepared.write_text(json.dumps({'schema_version':1,'executable':str(self.exe),'runtime_identity_sha256':identity,'uv_sha256':'a'*64,'lock_sha256':'b'*64}));prepared.chmod(0o600)
  value=json.loads(self.pointer.read_text());value.update(executable=str(self.exe),runtime_identity_sha256=identity);value['attestation']['runtime_identity_sha256']=identity;self.pointer.write_text(json.dumps(value))
 def proof(self,executable,adapter,model):
  identity=R.probe_runtime(executable,managed_root=self.root)['runtime_identity_sha256']
  return {'probe':{'runtime_identity_sha256':identity},'attestation':{'schema_version':1,'runtime_identity_sha256':identity,'base_model_sha256':model_identity(model),'adapter_sha256':hashlib.sha256(Path(adapter).read_bytes()).hexdigest(),'data_kind':'synthetic_compact_wiki','checks':dict.fromkeys(R.CHECKS,True)}}
 def queue(self):
  queue=self.home/'queue';queue.mkdir(mode=0o700,exist_ok=True);return queue
 def test_adapter_only_drift_is_reattested_by_session_maintenance(self):
  import functools,setup_readiness
  from context_learning.runtime_installer import reattest_active
  self.prepare();self.generation();self.stale_adapter();before=json.loads(self.pointer.read_text())
  self.assertIn('laya_current_adapter_required',setup_entry.check_update(self.project)['reasons'])
  row=setup_readiness.refresh_adapter_attestation(self.queue(),reattest=functools.partial(reattest_active,prover=self.proof,probe=R.probe_runtime))
  self.assertEqual(row['status'],'reattested',row)
  after=json.loads(self.pointer.read_text());self.assertEqual(after['executable'],before['executable']);self.assertEqual(after['model_dir'],before['model_dir'])
  self.assertEqual(after['adapter_path'],str(self.adapter))
  row=setup_entry.check_update(self.project);self.assertEqual(row['status'],'up_to_date',row);self.assertTrue(row['layaReady'])
 def test_adapter_drift_with_other_drift_is_left_for_review(self):
  import setup_readiness
  self.prepare();self.stale_adapter();(self.model/'model.safetensors').unlink();before=self.pointer.read_bytes()
  self.assertEqual(setup_readiness.laya_preflight()['reason'],'laya_local_model_required')
  row=setup_readiness.refresh_adapter_attestation(self.queue(),reattest=lambda *a,**k:self.fail('must not re-attest'))
  self.assertEqual(row['status'],'not_required');self.assertEqual(self.pointer.read_bytes(),before)
 def test_failed_refresh_is_not_retried_and_keeps_notice(self):
  import contextlib,io,setup_readiness
  self.prepare();self.stale_adapter();calls=[]
  def broken(*a,**k):calls.append(a);raise ValueError('managed_compatibility_failed')
  queue=self.queue()
  self.assertEqual(setup_readiness.refresh_adapter_attestation(queue,reattest=broken)['status'],'failed')
  self.assertEqual(setup_readiness.refresh_adapter_attestation(queue,reattest=broken)['status'],'failed_previously');self.assertEqual(len(calls),1)
  out=io.StringIO()
  with patch('qmd_route.project_paths',return_value={'shared':True}),contextlib.redirect_stdout(out):update_hook_queue._setup_notice(self.project,queue,{'status':'ready'})
  self.assertIn('laya_current_adapter_required',out.getvalue())
 def test_resource_deferred_refresh_is_retried_without_notice(self):
  import contextlib,io,setup_readiness
  from resource_deferred import ResourceDeferred
  self.prepare();self.stale_adapter();queue=self.queue()
  def busy(*a,**k):raise ResourceDeferred('trainer_system_pressure')
  self.assertEqual(setup_readiness.refresh_adapter_attestation(queue,reattest=busy)['status'],'deferred')
  self.assertEqual(setup_readiness.refresh_adapter_attestation(queue,reattest=busy)['status'],'deferred')
  out=io.StringIO()
  with patch('qmd_route.project_paths',return_value={'shared':True}),contextlib.redirect_stdout(out):update_hook_queue._setup_notice(self.project,queue,{'status':'ready'})
  self.assertEqual(out.getvalue(),'')

if __name__=='__main__':unittest.main(verbosity=2)
