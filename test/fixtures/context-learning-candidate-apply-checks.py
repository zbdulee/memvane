"""Real private DB/journal/CAS, fake admitted labels and model transport."""
from contextlib import contextmanager
from pathlib import Path
import json,tempfile,time,unittest,sys,threading
from unittest.mock import patch
from context_learning import shared,automatic_staging,automatic_cohort,learning_profile,runtime_authorization,inference_identity,project_scope
from context_learning.learning_queue import LearningQueue
from context_learning.store import canonical
try:from context_learning import candidate_apply as A
except ImportError:A=None

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700);self.project=self.root/'project';self.project.mkdir(mode=0o700)
  self.addCleanup(patch.stopall)
  @contextmanager
  def db(_):
   with shared._db_at(self.root/shared.DATABASE_NAME) as conn:yield conn
  patch.object(shared,'_db',side_effect=db).start();self.db=db
  patch.object(shared,'_case_active',return_value=True).start()
  self.grant={'scope':project_scope.SCOPE,'projectRoot':str(self.project),'operations':['quality_evaluate'], 'cohort':{'schema':project_scope.COHORT_SCHEMA,'id':'test','minQualityTrainCases':100,'minTrainFamilies':5,'minValidationCases':50,'minValidationFamilies':5,'minEmptyValidation':10}}
  self.profile={'autoApply':True,'runtimeApprovalSha256':runtime_authorization.proposal_sha256(self.grant),'view':'tool-edit-8192-choice-v1','modelIdentitySha256':'a'*64,'maxQualityAgeSeconds':3600,'projectRoot':str(self.project)}
  self.scope=patch.object(learning_profile,'require',return_value=self.profile).start()
  patch.object(runtime_authorization,'require_operation',return_value=self.grant).start()
  self.policy={'baseArtifact':str(self.root/'base.artifact'),'targetExact':.9,'modelInputView':self.profile['view']}
  self.write(self.root/'base.artifact',b'base')
  self.old={'schema_version':1,'artifact_path':str(self.root/'base.artifact'),'artifact_sha256':shared._hash(self.root/'base.artifact'),'cycle_id':'base','model_input_view':self.profile['view']}
  self.json(self.root/'active-checkpoint.json',self.old)
  self.receipt={'schema':inference_identity.SCHEMA,'view':self.profile['view'],'verified':{'base_model_sha256':'a'*64},'checkpoint_sha256':learning_profile.digest(self.old),'createdAt':1}
  self.json(self.root/inference_identity.RECEIPT,self.receipt)
  self.json(self.root/inference_identity.BINDING,{'schema':inference_identity.SCHEMA,'receipt_sha256':learning_profile.digest(self.receipt)})
  self.train=[self.row(i,'train') for i in range(100)];self.validation=[self.row(i,'val') for i in range(50)]
  patch.object(automatic_cohort,'training_and_validation',return_value=(self.train,self.validation)).start()
  patch.object(automatic_staging,'training_and_validation',return_value=(self.train,self.validation)).start()
  queue=LearningQueue(self.root);job=queue.plan(self.train,approval=runtime_authorization.proposal_sha256(self.grant),cohort='test',family='family',minimum=5,maximum=100)
  self.cycle=job['id'];folder=self.root/'cycles'/self.cycle;folder.mkdir(mode=0o700,parents=True)
  folder.parent.chmod(0o700)
  self.write(folder/'candidate.artifact',b'candidate')
  score={'questions':50,'exact_rate':1.0,'necessary_recall':1.0,'irrelevant_injection_rate':0.0,'none_exact_rate':1.0}
  self.stage={'authorizationSha256':job['approval'],'sourceActiveAtStage':True,'artifactPath':str(folder/'candidate.artifact'),'artifactSha256':shared._hash(folder/'candidate.artifact'),'baseArtifactSha256':shared._hash(self.policy['baseArtifact']),'trainEvidenceSha256':automatic_staging._case_evidence(self.train),'model_input_view':self.profile['view'],'trainCases':[{'projectRoot':r[7],'requestId':r[1],'inputSha256':r[2]} for r in self.train], 'validationCases':[{'projectRoot':r[7],'requestId':r[1],'inputSha256':r[2]} for r in self.validation], 'metrics':{'incumbent':score,'candidate':score,'validation_questions':50,'empty_validation':10,'evaluatedAt':time.time(),'validationEvidenceSha256':automatic_staging._case_evidence(self.validation),'incumbentArtifactSha256':self.old['artifact_sha256'],'applied':False}}
  with db(None) as conn:conn.execute('INSERT INTO cycles VALUES (?,?,?,?,NULL)',(self.cycle,'staged_evaluated',canonical(self.stage),time.time()))
  self.events=[];self.low=False
 def row(self,i,kind):
  rid=f'{kind}-{i}';sample={'prompt':{'text':rid},'candidates':[{'candidate_id':rid,'revision_sha256':'a'*64}]}
  encoded=canonical(sample)
  from context_learning.contracts import digest
  sha=digest(encoded)
  return ('p',rid,sha,encoded,canonical({rid:rid}),canonical({'selected_ids':[] if i<10 and kind=='val' else [rid],'input_sha256':sha}),'train' if kind=='train' else 'validation',str(self.project),'corpus','[]',None,0,f'{kind}/{i%5}/{i}.md')
 def write(self,p,data):p.write_bytes(data);p.chmod(0o600)
 def json(self,p,data):self.write(p,(canonical(data)+'\n').encode())
 def transport(self,command,record):
  from context_learning.local_cycle import ResourceDeferred
  self.events.append(command)
  if self.low:raise ResourceDeferred('fixture memory')
  return {'status':'prepared' if command=='prepare' else 'activated','token':record['apply_id'],'generation':record['new_pointer']['artifact_sha256']}
 def run_apply(self,**kw):
  self.assertIsNotNone(A,'candidate application missing')
  return A.apply_candidate(self.root,self.policy,self.cycle,transport=self.transport,**kw)
 def test_success_and_repeated_wake_never_republishes_or_trains(self):
  result=self.run_apply();self.assertEqual(result['status'],'applied')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text())['artifact_sha256'],self.stage['artifactSha256'])
  A.readable(self.root)
  events=self.events.copy();self.assertEqual(self.run_apply()['status'],'already_applied');self.assertEqual(self.events,events)
 def test_no_apply_authority_or_resource_preserves_old_pointer(self):
  self.low=True;answer=self.run_apply();self.assertEqual(answer['status'],'apply_deferred')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
  self.scope.side_effect=ValueError('revoked')
  answer=self.run_apply();self.assertEqual(answer['status'],'apply_blocked')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
 def test_failed_quality_or_stale_source_preserves_old(self):
  self.stage['metrics']['candidate']={**self.stage['metrics']['candidate'],'exact_rate':.5}
  with self.db(None) as conn:conn.execute('UPDATE cycles SET evidence=?',(canonical(self.stage),))
  answer=self.run_apply();self.assertEqual(answer['status'],'apply_blocked');self.assertEqual(self.events,[])
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
 def test_foreign_pointer_change_is_not_overwritten(self):
  def changed(phase):
   if phase=='prepared':self.json(self.root/'active-checkpoint.json',{**self.old,'cycle_id':'foreign'})
  answer=self.run_apply(fault=changed);self.assertEqual(answer['status'],'apply_blocked')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text())['cycle_id'],'foreign')
 def test_crash_recovery_for_each_durable_boundary(self):
  self.assertIsNotNone(A,'candidate application missing')
  for phase in ('intent','service_prepared','prepared','pointer_written','receipt_written','binding_written','pointer_committed','service_activated','activated'):
   with self.subTest(phase=phase):
    # Each boundary gets its own isolated namespace and unchanged synthetic evidence.
    other=Checks('test_success_and_repeated_wake_never_republishes_or_trains');other.setUp()
    try:
     def crash(at):
      if at==phase:raise RuntimeError('fixture crash')
     with self.assertRaises(RuntimeError):other.run_apply(fault=crash)
     pointer=json.loads((other.root/'active-checkpoint.json').read_text())
     if pointer!=other.old and phase!='activated':
      with self.assertRaises(ValueError):A.readable(other.root)
     self.assertIn(other.run_apply()['status'],('applied','already_applied'));A.readable(other.root)
     with other.db(None) as conn:self.assertEqual(conn.execute('SELECT status FROM cycles WHERE cycle_id=?',(other.cycle,)).fetchone()[0],'applied')
    finally:other.doCleanups()
 def test_worker_rechecks_exact_committed_profile_candidate_and_generation(self):
  from context_learning import inference_service as service
  def crash(phase):
   if phase=='pointer_committed':raise RuntimeError('fixture crash')
  with self.assertRaises(RuntimeError):self.run_apply(fault=crash)
  row=A.read_journal(self.root)
  policy={**self.policy,'trainerArgv':[sys.executable,'/fake/adapter',str(self.root/'model')]}
  envelope={'command':'apply_activate','schema':service.PROTOCOL,'signature':service._signature(),
    'state_dir':str(self.root),'namespace':service.namespace(self.root),'apply_id':row['apply_id'],
    'queued_at_epoch':time.time(),'deadline_epoch':time.time()+10}
  verified={'runtime_identity_sha256':'runtime','base_model_sha256':'a'*64}
  with patch.object(shared,'_policy',return_value=policy),patch.object(service,'_policy',return_value=(policy,policy)),patch.object(service._IDENTITY_CACHE,'verify',return_value=verified):
   job=service._validate_apply(envelope)
   self.assertEqual(job['apply_id'],row['apply_id']);self.assertEqual(job['generation'][-1],self.stage['artifactSha256'])
   with self.assertRaises(service.ServiceFailure):service._validate_apply({**envelope,'namespace':'foreign'})
 def test_ordinary_worker_final_revalidation_rejects_profile_revoked_during_forward(self):
  from context_learning import inference_service as service
  self.run_apply()
  live={'schema':'qmd-live-selector-v1','trainerArgv':[sys.executable,'/fake/adapter','/fake/model'],'maxRssMib':4096,'timeoutSeconds':10}
  self.json(self.root/'live-selector.json',live);self.json(self.root/learning_profile.NAME,{'proposal':{'projectRoot':str(self.project)}})
  case={'request_id':'synthetic','prompt':'Which synthetic marker?', 'candidates':[{'id':'card','revision_sha256':'a'*64,'input_text':'Synthetic marker.','input_kind':'full_compact_wiki_body'}]}
  case['input_sha256']=learning_profile.digest(case)
  request={'schema_version':1,'artifact_path':self.stage['artifactPath'],'model_input_view':self.profile['view'],'cases':[case]}
  env=service._make_envelope(self.root,request,self.stage['artifactSha256'],time.monotonic()+10,'active-checkpoint.json')
  verified={'runtime_identity_sha256':'runtime','base_model_sha256':'a'*64};allowed=[True]
  with patch.object(service,'_policy',return_value=(live,live)),patch.object(service._IDENTITY_CACHE,'verify',return_value=verified),patch.object(learning_profile,'selection_enabled',side_effect=lambda *a:allowed[0]):
   job=service._validate_request(env)
   def forward(*a,**kw):allowed[0]=False;return {}
   engine=service.GenerationEngine(load=lambda _:None,forward=forward,admission=lambda *a,**kw:({},{}),rss=lambda:100)
   with self.assertRaises(ValueError):engine.run(job,cancelled=lambda:False,revalidate=lambda:service._validate_request(env))
 def test_stale_completed_quality_rechecks_without_a_training_call(self):
  from context_learning.resource_coordination import Coordinator
  from types import SimpleNamespace
  self.stage['metrics']['evaluatedAt']=time.time()-7200
  with self.db(None) as conn:conn.execute('UPDATE cycles SET evidence=?',(canonical(self.stage),))
  policy={**self.policy,'maxRssMib':4096,'minAvailableMib':512,'maxLoadPerCpu':1}
  score=self.stage['metrics']['candidate']
  report={'ready':True,'total_mib':16384,'available_mib':10000,'load_per_cpu':.1,
          'observed_at_mono':time.monotonic(),'query_status':{'memory_pressure':'ok'},
          'memory_pressure':{'level':'normal','system_free_percent':60}}
  coord=self.root/'controller';coord.mkdir(mode=0o700)
  with patch.object(learning_profile,'present',return_value=True),patch.object(shared,'resource_report',return_value=report),patch.object(shared,'_predict',return_value=(score,{})),patch.object(automatic_staging,'require_operation',return_value=self.grant):
   result=automatic_staging.evaluate_pending(self.root,policy,self.cycle,trainer_factory=lambda _:SimpleNamespace(),coordinator=Coordinator(coord))
  self.assertEqual(result['status'],'staged_evaluated',result)
  self.assertGreater(result['metrics']['evaluatedAt'],time.time()-30)
 def test_multi_irrelevant_incumbent_can_be_replaced_by_verified_improvement(self):
  self.stage['metrics']['incumbent']={**self.stage['metrics']['incumbent'],'irrelevant_injection_rate':2.0}
  with self.db(None) as conn:conn.execute('UPDATE cycles SET evidence=?',(canonical(self.stage),))
  self.assertEqual(self.run_apply()['status'],'applied')
 def test_revocation_after_pointer_commit_stays_closed_without_automatic_rollback(self):
  def crash(phase):
   if phase=='pointer_written':raise RuntimeError('fixture crash')
  with self.assertRaises(RuntimeError):self.run_apply(fault=crash)
  committed=json.loads((self.root/'active-checkpoint.json').read_text())
  self.scope.side_effect=ValueError('revoked')
  self.assertEqual(self.run_apply()['status'],'apply_blocked')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),committed)
  with self.assertRaises(ValueError):A.readable(self.root)
 def test_corrupt_prepared_journal_cannot_select_an_unvalidated_artifact(self):
  def crash(phase):
   if phase=='prepared':raise RuntimeError('fixture crash')
  with self.assertRaises(RuntimeError):self.run_apply(fault=crash)
  row=A.read_journal(self.root)
  row['new_pointer']={**row['new_pointer'],'artifact_path':self.old['artifact_path'],'artifact_sha256':self.old['artifact_sha256']}
  row['new_receipt']['checkpoint_sha256']=learning_profile.digest(row['new_pointer'])
  self.json(self.root/A.JOURNAL,row)
  self.assertEqual(self.run_apply()['status'],'apply_blocked')
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
 def test_rejected_quality_does_not_starve_next_fresh_batch(self):
  from context_learning.evaluation_queue import EvaluationQueue
  import config
  self.profile['localTrain']=True
  evaluations=EvaluationQueue(self.root);job=evaluations.ensure(self.cycle,'pin','val');evaluations.finish(job['id'],'blocked')
  cfg={'sharedLearning':{'enabled':True,'root':str(self.root),'profile':learning_profile.SCHEMA}}
  policy={**self.policy,'_runtime_authorization_v2':True}
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'_policy',return_value=policy),patch.object(learning_profile,'present',return_value=True),patch.object(shared,'train_due',return_value={'status':'waiting_fresh_batch'}) as train,patch.object(automatic_staging,'evaluate_pending',side_effect=AssertionError('blocked candidate retried')):
   self.assertEqual(shared.run_training_pending(self.root,self.project)['status'],'waiting_fresh_batch');train.assert_called_once()
 def test_waiting_small_candidate_does_not_starve_new_quality_sized_batch(self):
  from context_learning.evaluation_queue import EvaluationQueue
  import config
  self.profile['localTrain']=True
  queue=EvaluationQueue(self.root);job=queue.ensure(self.cycle,'pin','val');queue.finish(job['id'],'waiting_quality')
  cfg={'sharedLearning':{'enabled':True,'root':str(self.root),'profile':learning_profile.SCHEMA}}
  policy={**self.policy,'_runtime_authorization_v2':True}
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'_policy',return_value=policy),patch.object(learning_profile,'present',return_value=True),patch.object(automatic_staging,'evaluate_pending',return_value={'status':'staged_waiting_quality_cohort','cycle_id':self.cycle}),patch.object(shared,'train_due',return_value={'status':'waiting_fresh_batch'}) as train:
   self.assertEqual(shared.run_training_pending(self.root,self.project)['status'],'waiting_fresh_batch');train.assert_called_once()
 def test_apply_resource_backoff_is_honored_by_direct_and_hook_recovery(self):
  self.low=True;self.assertEqual(self.run_apply()['status'],'apply_deferred')
  before=self.events.copy();self.low=False
  self.assertEqual(self.run_apply()['status'],'apply_deferred');self.assertEqual(self.events,before)
  retry=A.read_journal(self.root)['retry_at']
  with patch.object(A.time,'time',return_value=retry+1):self.assertEqual(self.run_apply()['status'],'applied')
 def test_staged_fallback_never_dispatches_foreign_namespace_in_shared_history(self):
  import config
  self.profile['localTrain']=True
  other=self.root/'other-namespace';other.mkdir(mode=0o700)
  foreign=LearningQueue(other).plan(self.train,approval=runtime_authorization.proposal_sha256(self.grant),cohort='test',family='family',minimum=5,maximum=100)
  with self.db(self.root) as db:
   db.execute("UPDATE cycles SET status='applied' WHERE cycle_id=?",(self.cycle,))
   db.execute("INSERT INTO cycles VALUES (?,'staged',?,?,NULL)",(foreign['id'],canonical(self.stage),time.time()+10))
  cfg={'sharedLearning':{'enabled':True,'root':str(self.root),'profile':learning_profile.SCHEMA}}
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'_policy',return_value={**self.policy,'_runtime_authorization_v2':True}),patch.object(learning_profile,'present',return_value=True),patch.object(automatic_staging,'evaluate_pending',side_effect=AssertionError('foreign stage dispatched')),patch.object(shared,'train_due',return_value={'status':'waiting_fresh_batch'}) as train:
   self.assertEqual(shared.run_training_pending(self.root,self.project)['status'],'waiting_fresh_batch');train.assert_called_once()
 def test_symlinked_cycles_never_creates_a_lock_outside_namespace(self):
  saved=self.root/'saved-cycles';(self.root/'cycles').rename(saved)
  outside=self.root/'outside';outside.mkdir(mode=0o700);(self.root/'cycles').symlink_to(outside,target_is_directory=True)
  self.assertEqual(self.run_apply()['status'],'apply_blocked')
  self.assertEqual(list(outside.iterdir()),[])
 def test_transient_service_transport_uses_backoff_without_rejecting_candidate(self):
  import socket
  from context_learning import inference_service as service
  self.policy['trainerArgv']=[sys.executable,'/fake/adapter',str(self.root/'model')]
  for where,error in (('_ensure',service.ServiceFailure('starting')),('_exchange',socket.timeout()),('_exchange',service.ServiceFailure('deadline_expired'))):
   with self.subTest(where=where,error=type(error).__name__):
    with patch.object(service,'service_root',return_value=self.root),patch.object(service,'_ensure'),patch.object(service,'_exchange',return_value={'status':'prepared'}),patch.object(service,where,side_effect=error):
     answer=A.apply_candidate(self.root,self.policy,self.cycle)
    self.assertEqual(answer['status'],'apply_deferred',answer)
    row=A.read_journal(self.root);self.assertNotEqual(row['phase'],'rejected');row['retry_at']=0;self.json(self.root/A.JOURNAL,row)
  with patch.object(service,'service_root',return_value=self.root),patch.object(service,'_ensure'),patch.object(service,'_exchange',return_value={'status':'queue_full'}):
   answer=A.apply_candidate(self.root,self.policy,self.cycle)
  self.assertEqual(answer['status'],'apply_deferred');self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
 def test_service_identity_and_permission_errors_still_reject_candidate(self):
  from context_learning import inference_service as service
  self.policy['trainerArgv']=[sys.executable,'/fake/adapter',str(self.root/'model')]
  for code in ('version_mismatch','permission_denied'):
   with self.subTest(code=code),patch.object(service,'service_root',return_value=self.root),patch.object(service,'_ensure',side_effect=service.ServiceFailure(code)):
    answer=A.apply_candidate(self.root,self.policy,self.cycle)
   self.assertEqual(answer['status'],'apply_blocked');self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
   (self.root/A.JOURNAL).unlink()
 def test_revoked_source_before_preparation_preserves_incumbent(self):
  with patch.object(shared,'_case_active',return_value=False):answer=self.run_apply()
  self.assertEqual(answer['status'],'apply_blocked');self.assertEqual(self.events,[])
  self.assertEqual(json.loads((self.root/'active-checkpoint.json').read_text()),self.old)
 def test_real_private_ipc_publication_uses_prepared_object_and_same_generation(self):
  from context_learning import inference_service as service
  with tempfile.TemporaryDirectory(prefix='qa-',dir='/tmp') as temp:
   controller=Path(temp).resolve();controller.chmod(0o700);ready=threading.Event()
   policy={**self.policy,'trainerArgv':[sys.executable,'/fake/adapter',str(self.root/'model')],'maxRssMib':4096,'timeoutSeconds':30}
   live={**policy,'schema':'qmd-live-selector-v1'};self.json(self.root/'live-selector.json',live)
   verified={'runtime_identity_sha256':'runtime','base_model_sha256':'a'*64}
   engine=service.GenerationEngine(load=lambda job:job['generation'][-1],forward=lambda loaded,job,**kw:{'synthetic':loaded},admission=lambda *a,**kw:({}, {'available_mib':10000}),rss=lambda:100)
   engine.active=(('runtime','a'*64,self.profile['view'],self.old['artifact_sha256']),self.old['artifact_sha256']);engine.footprint_mib=256
   with patch.object(shared,'_policy',return_value=policy),patch.object(service,'_policy',return_value=(live,live)),patch.object(service._IDENTITY_CACHE,'verify',return_value=verified),patch.object(service,'service_root',return_value=controller):
    worker=threading.Thread(target=service._serve,args=(controller,),kwargs={'engine':engine,'ready':ready},daemon=True)
    worker.start();self.assertTrue(ready.wait(2))
    try:
     answer=A.apply_candidate(self.root,policy,self.cycle)
     self.assertEqual(answer['status'],'applied',answer);self.assertEqual(engine.active[0][-1],self.stage['artifactSha256']);A.readable(self.root)
    finally:
     deadline=time.monotonic()+3
     while time.monotonic()<deadline:
      stopped=service._exchange(controller,{'command':'shutdown'},deadline)
      if stopped=={'status':'stopped'}:break
      time.sleep(.005)
     worker.join(2);self.assertFalse(worker.is_alive())

if __name__=='__main__':unittest.main()
