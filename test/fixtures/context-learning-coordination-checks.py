"""Synthetic resource/compute leases and durable update recovery; no ML."""
from pathlib import Path
from contextlib import contextmanager
import json,os,subprocess,sys,tempfile,time,unittest
from context_learning.resource_coordination import Coordinator,decision
from context_learning.local_cycle import ResourceDeferred
from context_learning.training_cursor import UpdateCursor,record_completion,completion,run_updates

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700);self.c=Coordinator(self.root)
  self.policy={'maxRssMib':4096,'minAvailableMib':512,'maxLoadPerCpu':1}
  self.report={'ready':True,'total_mib':16384,'available_mib':10000,'load_per_cpu':.1,'observed_at_mono':time.monotonic(),'query_status':{'memory_pressure':'ok'},'memory_pressure':{'system_free_percent':70,'level':'normal'}}
 def test_missing_slots_do_not_repeat_metadata_queries_at_every_hash_checkpoint(self):
  from collections import Counter
  from unittest.mock import patch
  probes=[];real_stat=os.stat;real_lstat=os.lstat
  def probe(fn,path,*args,**kwargs):
   if Path(path).parent==self.root and Path(path).name.startswith('inference-'):
    probes.append(str(path))
   return fn(path,*args,**kwargs)
  with patch('os.stat',side_effect=lambda *a,**k:probe(real_stat,*a,**k)),patch('os.lstat',side_effect=lambda *a,**k:probe(real_lstat,*a,**k)):
   self.assertFalse(self.c.demand())
  self.assertLessEqual(max(Counter(probes).values(),default=0),1)
 def test_last_slot_lease_is_observed_without_caching_absence(self):
  import fcntl
  from context_learning.resource_coordination import SLOTS
  self.assertFalse(self.c.demand())
  fd=os.open(self.root/f'inference-{SLOTS-1}.lock',os.O_CREAT|os.O_RDWR,0o600)
  try:
   fcntl.flock(fd,fcntl.LOCK_EX);self.assertTrue(self.c.demand())
  finally:os.close(fd)
  self.assertFalse(self.c.demand())
 def test_probe_permission_errors_are_not_absence(self):
  from unittest.mock import patch
  with patch('os.stat',side_effect=PermissionError('denied')),patch('os.lstat',side_effect=PermissionError('denied')):
   with self.assertRaises(PermissionError):self.c.demand()
 def test_dangling_state_links_still_refuse_admission(self):
  for name,reason in (('cleanup-reservation.json','qmd_cleanup_recovery_required'),('startup-reservation.json','qmd_startup_recovery_required'),('resource-recovery.json','resource_recovery_unknown')):
   path=self.root/name;path.symlink_to(self.root/'missing')
   try:
    with self.assertRaisesRegex(ResourceDeferred,reason):self.c.check(self.policy,self.report)
   finally:path.unlink()
 def test_queued_inference_defers_and_expired_metadata_cannot_steal_live_lease(self):
  with self.c.inference(time.monotonic()+1,'ns'):
   self.assertTrue(self.c.demand())
   with self.assertRaisesRegex(ResourceDeferred,'learning_deferred_inference'):
    with self.c.training(self.policy,self.report):pass
  self.assertFalse(self.c.demand())
 def test_only_one_trainer_and_inference_deadline_does_not_wait_for_batch(self):
  with self.c.training(self.policy,self.report):
   with self.assertRaises(ResourceDeferred):
    with self.c.training(self.policy,self.report):pass
   with self.c.step(self.policy,self.report):
    with self.assertRaisesRegex(ResourceDeferred,'learning_inference_deadline'):
     with self.c.inference_compute(time.monotonic()+.01):pass
 def test_resource_unknown_cpu_and_memory_defer_or_shrink(self):
  self.assertTrue(decision(self.policy,self.report)['ready'])
  self.assertEqual(decision(self.policy,{**self.report,'load_per_cpu':.8})['microbatch_size'],1)
  self.assertFalse(decision(self.policy,{**self.report,'load_per_cpu':1.2})['ready'])
  self.assertFalse(decision(self.policy,{**self.report,'available_mib':100})['ready'])
  self.assertFalse(decision(self.policy,{**self.report,'total_mib':None})['ready'])
 def test_inference_arrives_after_update_and_prevents_next_update(self):
  with self.c.training(self.policy,self.report):
   with self.c.step(self.policy,self.report):pass
   with self.c.inference(time.monotonic()+1,'other'):
    with self.assertRaises(ResourceDeferred):
     with self.c.step(self.policy,self.report):self.fail('next training update ran')
    with self.c.inference_compute(time.monotonic()+1):pass
 def test_reboot_or_crash_releases_os_claim_without_pid_checks(self):
  child=subprocess.Popen([sys.executable,'-c',"import os,fcntl,sys; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()",str(self.root/'inference-0.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
  try:
   self.assertEqual(child.stdout.readline().strip(),'ready');self.assertTrue(self.c.demand())
  finally:child.communicate('');child.wait(timeout=2)
  self.assertFalse(self.c.demand())
 def test_checkpoint_resume_and_immutable_completion_no_second_train(self):
  request={'schema_version':1,'artifact_path':str(self.root/'candidate.artifact'),'train':[{'synthetic':True}]}
  cursor=UpdateCursor(request,[[1],[2]])
  def save(path):path.write_bytes(b'synthetic-head');path.chmod(0o600)
  cursor.commit(1,save,{'cpu':[1,2],'mps':None})
  resumed=UpdateCursor(request,[[1],[2]]);self.assertEqual(resumed.step,1)
  self.assertEqual(resumed.head().read_bytes(),b'synthetic-head')
  resumed.commit(2,save,{'cpu':[3,4],'mps':None})
  artifact=Path(request['artifact_path']);save(artifact);record_completion(request);self.assertIsNotNone(completion(request))
  artifact.write_bytes(b'changed')
  with self.assertRaises(ValueError):completion(request)
 def test_committed_updates_resume_without_replaying_or_new_teacher(self):
  request={'artifact_path':str(self.root/'candidate.artifact')};groups=[[1],[2],[3]];calls=[]
  c=self.c;policy=self.policy;report=self.report
  class Guard:
   @contextmanager
   def step(inner):
    with c.step(policy,report) as decision:yield decision
  def save(path):path.write_bytes(str(calls[-1]).encode());path.chmod(0o600)
  def update(group,size):calls.append(group[0]);return 0.1
  cursor=UpdateCursor(request,groups)
  with self.assertRaisesRegex(ValueError,'learning_yield'):
   run_updates(cursor,groups,Guard(),update,save,lambda:{'cpu':[1],'mps':None},per_run=1)
  self.assertEqual(calls,[1])
  resumed=UpdateCursor(request,groups)
  with self.c.inference(time.monotonic()+1,'priority'):
   with self.assertRaises(ResourceDeferred):run_updates(resumed,groups,Guard(),update,save,lambda:{'cpu':[1],'mps':None})
  self.assertEqual(calls,[1])
  run_updates(resumed,groups,Guard(),update,save,lambda:{'cpu':[1],'mps':None})
  self.assertEqual(calls,[1,2,3])
  run_updates(UpdateCursor(request,groups),groups,Guard(),update,save,lambda:{'cpu':[1],'mps':None})
  self.assertEqual(calls,[1,2,3])
 def test_expired_inference_metadata_with_live_kernel_lease_still_defers(self):
  with self.c.inference(time.monotonic()+.01,'ns'):
   time.sleep(.02)
   self.assertTrue(self.c.demand())
   with self.assertRaises(ResourceDeferred):
    with self.c.training(self.policy,self.report):pass
 def test_adaptive_memory_reduces_microbatch_to_one(self):
  # Legacy profile retains its configured ratio reserve, with batch1 default.
  policy={**self.policy,'adaptiveMemory':{'device':'mps','fixedMib':1000,'perPairMib':1000,'estimateSource':'fixture','modelIdentitySha256':'a'*64}}
  report={**self.report,'available_mib':4000,'query_status':{'memory_pressure':'ok'},'memory_pressure':{'system_free_percent':50,'level':'normal'}}
  self.assertEqual(decision(policy,report)['microbatch_size'],1)
  self.assertFalse(decision(policy,{**report,'available_mib':3000})['ready'])
 def test_loaded_model_credit_is_measured_and_cannot_spend_os_reserve(self):
  policy={**self.policy,'adaptiveMemory':{'device':'mps','fixedMib':3000,'perPairMib':500,'estimateSource':'fixture','modelIdentitySha256':'a'*64}}
  before={**self.report,'available_mib':7000,'query_status':{'memory_pressure':'ok'},'memory_pressure':{'system_free_percent':50,'level':'normal'}}
  self.assertTrue(decision(policy,before)['ready'])
  after={**before,'available_mib':4000}
  self.assertFalse(decision(policy,after)['ready'])
  self.assertTrue(decision(policy,after,resident_credit=3000)['ready'])
  self.assertFalse(decision(policy,{**after,'available_mib':1000},resident_credit=3000)['ready'])
 def test_pending_retry_backoff_drains_without_teacher_or_new_data(self):
  import sqlite3
  from contextlib import contextmanager
  from unittest.mock import patch
  from context_learning.learning_queue import LearningQueue
  from context_learning.learning_retry import wait_pending
  @contextmanager
  def connect():
   db=sqlite3.connect(self.root/'jobs.sqlite3')
   try:
    with db:yield db
   finally:db.close()
  q=LearningQueue(self.root,connect=connect)
  rows=[('owner',str(i),str(i),json.dumps({'prompt':{'text':str(i)},'candidates':[{'candidate_id':'card','revision_sha256':str(i)}]}),json.dumps({'card':str(i)}),json.dumps({'selected_ids':[]}),'train','/owner','x','[]',None,0,'source') for i in range(2)]
  job=q.plan(rows,approval='a',cohort='c',family='f',minimum=2,maximum=2)
  q.defer(job['id'],'inference',now=100);clock=[100.0];calls=[]
  def sleep(dt):clock[0]+=dt
  def run():
   calls.append('local-only');q.complete(job['id']);return {'status':'staged'}
  with patch('context_learning.learning_retry.LearningQueue',return_value=q):
   result=wait_pending(self.root,run,clock=lambda:clock[0],wall=lambda:clock[0],sleep=sleep)
   self.assertEqual(result['status'],'drained');self.assertEqual(calls,['local-only']);self.assertGreaterEqual(clock[0],130)
   wait_pending(self.root,lambda:self.fail('unchanged data retrained'),clock=lambda:clock[0],wall=lambda:clock[0],sleep=sleep)
 def test_completed_output_recovers_without_trainer_call(self):
  from context_learning.automatic_staging import _train_once
  request={'schema_version':1,'artifact_path':str(self.root/'candidate.artifact')}
  Path(request['artifact_path']).write_bytes(b'final');Path(request['artifact_path']).chmod(0o600);record_completion(request)
  class Never:
   def call(inner,*a,**kw):raise AssertionError('completed batch trained twice')
  answer,resources=_train_once(Never(),request,self.root)
  self.assertEqual(answer['status'],'trained');self.assertTrue(resources['recovered_completion'])
 def test_retry_glue_revalidates_scope_and_calls_only_local_train(self):
  from unittest.mock import patch
  import config
  from context_learning import shared,runtime_authorization
  cfg={'sharedLearning':{'enabled':True,'root':str(self.root)}}
  with patch.object(config,'find_project_config',return_value={'config':cfg}),patch.object(shared,'_safe_root',return_value=self.root),patch.object(shared,'_policy',return_value={'_runtime_authorization_v2':True}),patch.object(runtime_authorization,'require_operation',return_value={}) as grant,patch.object(shared,'train_due',return_value={'status':'staged'}) as train,patch.object(shared,'_run_impl',side_effect=AssertionError('teacher worker must not run')):
   self.assertEqual(shared.run_training_pending(self.root,self.root/'project')['status'],'staged')
   grant.assert_called_once_with(self.root,{'_runtime_authorization_v2':True},'local_train')
   train.assert_called_once()
   train.reset_mock();grant.side_effect=ValueError('revoked')
   self.assertEqual(shared.run_training_pending(self.root,self.root/'project')['status'],'runtime_approval_required')
   train.assert_not_called()
  with patch.object(config,'find_project_config',return_value={'config':{}}),patch.object(shared,'_policy',side_effect=AssertionError('disabled scope must stop')):
   self.assertEqual(shared.run_training_pending(self.root,self.root/'project')['status'],'disabled')
 def test_uncertain_or_dangling_final_artifact_is_not_retrained(self):
  from context_learning.automatic_staging import _train_once
  artifact=self.root/'candidate.artifact';outside=self.root/'outside';artifact.symlink_to(outside)
  class Never:
   def call(inner,*a,**kw):raise AssertionError('uncertain batch trained twice')
  with self.assertRaisesRegex(ValueError,'learning_completion_uncertain'):_train_once(Never(),{'artifact_path':str(artifact)},self.root)
  self.assertFalse(outside.exists())
 def test_dangling_pending_never_writes_external_target(self):
  request={'artifact_path':str(self.root/'candidate.artifact')};cursor=UpdateCursor(request,[[1]])
  target=self.root/'outside';(cursor.root/'head-1.pending').symlink_to(target)
  with self.assertRaises(ValueError):cursor.commit(1,lambda p:p.write_bytes(b'BAD'),{'cpu':[1],'mps':None})
  self.assertFalse(target.exists())
 def test_invalid_rng_cannot_commit_or_resume(self):
  request={'artifact_path':str(self.root/'candidate.artifact')}
  cursor=UpdateCursor(request,[[1]])
  def save(path):path.write_bytes(b'head');path.chmod(0o600)
  with self.assertRaisesRegex(ValueError,'learning_rng_invalid'):
   cursor.commit(1,save,{'cpu':None,'mps':None})
  cursor.commit(1,save,{'cpu':[1],'mps':None})
  row=json.loads(cursor.path.read_text());row['rng']={'cpu':['bad'],'mps':None}
  cursor.path.write_text(json.dumps(row))
  with self.assertRaisesRegex(ValueError,'learning_cursor_changed'):UpdateCursor(request,[[1]])
 def test_adapter_reloads_committed_head_rng_and_only_remaining_optimizer_groups(self):
  import importlib.util,math,types
  from unittest.mock import patch
  from context_learning import laya_adapter as adapter
  spec=importlib.util.spec_from_file_location('analytical_memory',Path(__file__).with_name('context-learning-adaptive-memory-checks.py'))
  helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
  Scalar=helpers.Scalar;Scalar.clone=lambda inner:Scalar(inner.value)
  models=[];optimizers=[];restored=[]
  def load(*a,**kw):
   model=helpers.TinyHead();models.append(model)
   param=types.SimpleNamespace(device='cpu',requires_grad=True,grad=None)
   model.parameters=lambda:iter([param]);model.train=model.eval=lambda:None
   model.encoder=types.SimpleNamespace(eval=lambda:None)
   class Weight:
    def detach(inner):return Scalar(model.weight)
   model.scorer=[types.SimpleNamespace(weight=Weight())]
   model.load_state_dict=lambda state,**kw:setattr(model,'weight',state['weight'])
   return model,None,{'max_len':8192}
  def optimizer(*a,**kw):
   result=helpers.Optimizer(models[-1]);result.state={};optimizers.append(result);return result
  torch=types.ModuleType('torch');torch.__path__=[];torch.long='long';torch.uint8='uint8'
  torch.set_num_threads=lambda n:None;torch.set_num_interop_threads=lambda n:None
  torch.get_num_threads=lambda:1;torch.get_num_interop_threads=lambda:1
  torch.manual_seed=lambda _:None;torch.optim=types.SimpleNamespace(SGD=optimizer)
  torch.tensor=lambda values,**kw:values
  torch.isfinite=lambda value:Scalar(math.isfinite(value.value));torch.equal=lambda x,y:x.value==y.value
  torch.get_rng_state=lambda:types.SimpleNamespace(tolist=lambda:[7,8])
  torch.set_rng_state=lambda values:restored.append(values)
  nn=types.ModuleType('torch.nn');nn.__path__=[];functional=types.ModuleType('torch.nn.functional')
  def cross_entropy(batch,targets):
   model=models[-1];ps=[1/(1+math.exp(-model.weight*r['x'])) for r in batch]
   loss=sum(-math.log(p if y else 1-p) for p,y in zip(ps,targets))/len(batch)
   grad=sum((p-y)*r['x'] for p,y,r in zip(ps,targets,batch))/len(batch)
   return Scalar(loss,lambda scale=1:setattr(model,'grad',model.grad+grad*scale))
  functional.cross_entropy=cross_entropy;nn.functional=functional;torch.nn=nn
  safetensors=types.ModuleType('safetensors');safetensors.__path__=[]
  safe_torch=types.ModuleType('safetensors.torch')
  safe_torch.load_file=lambda path:{'weight':float(Path(path).read_text())}
  class Guard:
   def __init__(inner,fail=None):inner.calls=0;inner.fail=fail
   def mark_loaded(inner,*a):pass
   def poll(inner):polls.append(1)
   @contextmanager
   def step(inner):
    inner.calls+=1
    if inner.calls==inner.fail:raise ValueError('learning_yield')
    yield {'microbatch_size':1}
  polls=[]
  items=[{'x':i+1,'gold_label':i%2,'ids':[1]*10,'candidate_id':str(i)} for i in range(5)]
  request={'schema_version':1,'artifact_path':str(self.root/'candidate.artifact'),
           'model_input_view':adapter.EDIT_INPUT_VIEW,'learning_control':{'synthetic':True},'train':[]}
  def save(path,model,*a):Path(path).write_text(str(model.weight));Path(path).chmod(0o600)
  with patch.dict(sys.modules,{'torch':torch,'torch.nn':nn,'torch.nn.functional':functional,
       'safetensors':safetensors,'safetensors.torch':safe_torch}),patch.object(adapter,'_training_guard',side_effect=[Guard(3),Guard()]),patch.object(adapter,'_allocation',return_value=(0,0)),patch.object(adapter,'model_identity',return_value='a'*64),patch.object(adapter,'_device',return_value='cpu'),patch.object(adapter,'_load',side_effect=load),patch.object(adapter,'_pairs',side_effect=lambda *a,**kw:items.copy()),patch.object(adapter,'_batch',side_effect=lambda group,*a:{'items':group}),patch.object(adapter,'_save_head',side_effect=save),patch.object(adapter,'_head_path',side_effect=lambda path,*a:path):
   with self.assertRaisesRegex(ValueError,'learning_yield'):adapter.train(self.root,request)
   self.assertFalse(Path(request['artifact_path']).exists())
   self.assertEqual(json.loads((self.root/'resume/cursor.json').read_text())['next_group'],1)
   answer=adapter.train(self.root,request)
  self.assertEqual(answer,{'schema_version':1,'status':'trained'})
  self.assertEqual([o.steps for o in optimizers],[1,2]);self.assertEqual(restored,[[7,8]])
  self.assertEqual([len(m.calls) for m in models],[2,3])
  self.assertIsNotNone(completion(request))
  self.assertGreaterEqual(len(polls),8)
if __name__=='__main__':unittest.main()
