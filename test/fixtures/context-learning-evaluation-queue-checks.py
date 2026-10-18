from pathlib import Path
from contextlib import contextmanager
import sqlite3,tempfile,unittest
try:from context_learning.evaluation_queue import EvaluationQueue
except ImportError:EvaluationQueue=None

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  @contextmanager
  def connect():
   db=sqlite3.connect(self.root/'fixture.sqlite3')
   try:
    with db:yield db
   finally:db.close()
  self.connect=connect
 def queue(self):
  self.assertIsNotNone(EvaluationQueue,'evaluation queue missing')
  return EvaluationQueue(self.root,connect=self.connect)
 def test_deferred_job_is_durable_and_duplicate_wake_does_not_steal_lease(self):
  q=self.queue();job=q.ensure('cycle','stage','validation')
  with q.claim(job['id'],now=100) as fd:
   self.assertIsInstance(fd,int)
   with q.claim(job['id'],now=100) as duplicate:self.assertIsNone(duplicate)
   q.defer(job['id'],'memory',now=100)
  reopened=self.queue();self.assertEqual(reopened.get(job['id'])['status'],'deferred')
  self.assertEqual(reopened.pending(now=100),[])
  with reopened.claim(job['id'],now=1000) as fd:self.assertIsInstance(fd,int)
 def test_waiting_quality_requires_changed_validation_or_explicit_refresh(self):
  q=self.queue();job=q.ensure('cycle','stage','validation');q.finish(job['id'],'waiting_quality')
  q.ensure('cycle','stage','validation');self.assertEqual(q.pending(),[])
  changed=q.ensure('cycle','stage','new-validation');self.assertEqual(changed['status'],'queued')
  q.finish(job['id'],'completed');self.assertEqual(q.pending(),[])
 def test_inherited_child_holds_evaluation_lease_after_parent_context_exits(self):
  import subprocess,sys
  q=self.queue();job=q.ensure('cycle','stage','validation')
  child=None
  try:
   with q.claim(job['id'],now=100) as fd:
    child=subprocess.Popen([sys.executable,'-c','import os,sys; os.fstat(int(sys.argv[1])); print("ready",flush=True); sys.stdin.buffer.read()',str(fd)],pass_fds=(fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    self.assertEqual(child.stdout.readline().strip(),'ready')
   with q.claim(job['id'],now=1000) as duplicate:self.assertIsNone(duplicate)
   child.stdin.close();self.assertEqual(child.wait(timeout=3),0);child.stdout.close()
   with q.claim(job['id'],now=1000) as resumed:self.assertIsInstance(resumed,int)
  finally:
   if child is not None:
    if child.stdin and not child.stdin.closed:child.stdin.close()
    child.wait(timeout=3)
    if child.stdout:child.stdout.close()
 def test_changed_stage_and_bounded_poison_jobs_fail_closed(self):
  q=self.queue();job=q.ensure('cycle','stage','validation')
  with self.assertRaises(ValueError):q.ensure('cycle','changed-stage','validation')
  for _ in range(3):q.defer(job['id'],'prediction_failed',failure=True,now=0)
  self.assertEqual(q.get(job['id'])['status'],'blocked');self.assertEqual(q.pending(),[])
 def test_shared_history_never_dispatches_or_mutates_another_namespace_job(self):
  other=self.root/'other';other.mkdir(mode=0o700)
  own=self.queue();foreign=EvaluationQueue(other,connect=self.connect)
  a=own.ensure('own','stage','validation');b=foreign.ensure('foreign','stage','validation')
  self.assertEqual([row['id'] for row in own.pending()],[a['id']])
  self.assertIsNone(own.get(b['id']))
  own.finish(b['id'],'blocked');own.refresh('foreign')
  with own.claim(b['id']) as denied:self.assertIsNone(denied)
  self.assertEqual(foreign.get(b['id'])['status'],'queued')
  with self.assertRaisesRegex(ValueError,'evaluation_job_scope_invalid'):own.defer(b['id'],'memory')
 def test_unscoped_predeployment_table_is_preserved_for_explicit_review(self):
  with self.connect() as db:
   db.execute('CREATE TABLE learning_evaluations(job_id TEXT PRIMARY KEY, status TEXT)')
   db.execute("INSERT INTO learning_evaluations VALUES ('old','deferred')")
  with self.assertRaisesRegex(ValueError,'evaluation_namespace_migration_required'):self.queue().pending()
  with self.connect() as db:self.assertEqual(db.execute('SELECT * FROM learning_evaluations').fetchall(),[('old','deferred')])
 def test_bounded_waiter_drains_quality_job_without_training_data(self):
  from unittest.mock import patch
  from context_learning.learning_queue import LearningQueue
  from context_learning.learning_retry import wait_pending
  q=self.queue();job=q.ensure('cycle','stage','validation');calls=[]
  def evaluate():calls.append('quality-only');q.finish(job['id'],'completed');return {'status':'staged_evaluated'}
  with patch('context_learning.learning_retry.LearningQueue',return_value=LearningQueue(self.root,connect=self.connect)):
   result=wait_pending(self.root,evaluate)
  self.assertEqual(calls,['quality-only']);self.assertEqual(result['status'],'drained')
 def test_terminal_quality_job_ignores_stale_defer_and_finish(self):
  q=self.queue();job=q.ensure('cycle','stage','validation')
  with q.claim(job['id'],now=100) as fd:
   self.assertIsInstance(fd,int);q.finish(job['id'],'completed')
  q.defer(job['id'],'memory_reserve',now=100)
  q.finish(job['id'],'waiting_quality');q.finish(job['id'],'blocked')
  self.assertEqual(q.get(job['id'])['status'],'completed')
  self.assertEqual(q.pending(now=float('inf')),[])
  q.refresh('cycle');self.assertEqual(q.get(job['id'])['status'],'queued')
  poisoned=q.ensure('poison','stage','validation')
  for _ in range(3):q.defer(poisoned['id'],'prediction_failed',failure=True,now=0)
  q.defer(poisoned['id'],'memory_reserve',now=0);q.finish(poisoned['id'],'completed')
  row=q.get(poisoned['id'])
  self.assertEqual((row['status'],row['failures'],row['reason']),('blocked',3,'prediction_failed'))
 def test_quality_pressure_defer_survives_restart_and_waiting_quality(self):
  q=self.queue();job=q.ensure('cycle','stage','validation')
  q.defer(job['id'],'memory_reserve',now=100)
  reopened=self.queue();row=reopened.get(job['id'])
  self.assertEqual((row['status'],row['reason'],row['failures'],row['retry_at']),('deferred','memory_reserve',0,130))
  self.assertEqual(reopened.pending(now=129),[])
  self.assertEqual(len(reopened.pending(now=130)),1)
  reopened.finish(job['id'],'waiting_quality');reopened.defer(job['id'],'interrupted',now=200)
  self.assertEqual(reopened.get(job['id'])['status'],'waiting_quality')
 def test_queue_summary_is_read_only_scoped_and_exposes_retry_reason(self):
  from context_learning.shared import _queue_summary
  from context_learning.learning_queue import LearningQueue
  own=self.queue();job=own.ensure('cycle','stage','validation');own.defer(job['id'],'memory_reserve',now=100)
  foreign=self.root/'foreign';foreign.mkdir(mode=0o700)
  other=EvaluationQueue(foreign,connect=self.connect)
  other_job=other.ensure('other','stage','validation');other.defer(other_job['id'],'cpu_pressure',now=100)
  db=sqlite3.connect((self.root/'fixture.sqlite3').as_uri()+'?mode=ro',uri=True)
  try:
   before=db.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall()
   quality=_queue_summary(db,'learning_evaluations',own.scope)
   self.assertEqual(quality,{'counts':{'deferred':1},'reasons':[{'status':'deferred','reason':'memory_reserve','count':1}],'next_retry':130})
   self.assertEqual(_queue_summary(db,'learning_jobs',own.scope),{})
   self.assertEqual(db.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall(),before)
  finally:db.close()
 def test_read_only_apply_validation_reads_selected_history_not_legacy_copy(self):
  from unittest.mock import patch
  from context_learning import shared,history_activation
  legacy=self.root/shared.DATABASE_NAME;selected=self.root/'selected.sqlite3'
  for path,value in ((legacy,'legacy'),(selected,'selected')):
   conn=sqlite3.connect(path);conn.execute('CREATE TABLE marker(value TEXT)');conn.execute('INSERT INTO marker VALUES (?)',(value,));conn.commit();conn.close();path.chmod(0o600)
  @contextmanager
  def routed(*a,**kw):yield selected
  with patch.object(history_activation,'writer_access',routed),shared.read_only_validation(),shared._db(self.root) as conn:
   self.assertEqual(conn.execute('SELECT value FROM marker').fetchone()[0],'selected')
 def test_actual_shared_quality_reports_none_recall_and_irrelevant_regressions(self):
  from context_learning import shared
  cases=[{'request_id':'a','input_sha256':'a','project_key':'p','selected_ids':['card'],'candidates':[{'id':'card'},{'id':'noise'}]},
         {'request_id':'b','input_sha256':'b','project_key':'p','selected_ids':[],'candidates':[{'id':'card'},{'id':'noise'}]}]
  answer={'predictions':[{'request_id':'a','input_sha256':'a','selected_ids':['card']},{'request_id':'b','input_sha256':'b','selected_ids':[]}]}
  good=shared._score(cases,answer)
  self.assertIn('none_exact_rate',good,'shared quality omits empty-choice gate')
  self.assertEqual(good['none_exact_rate'],1);self.assertEqual(good['necessary_recall'],1);self.assertEqual(good['irrelevant_injection_rate'],0)
  answer['predictions'][0]['selected_ids']=['noise'];answer['predictions'][1]['selected_ids']=['noise']
  bad=shared._score(cases,answer)
  self.assertEqual(bad['none_exact_rate'],0);self.assertEqual(bad['necessary_recall'],0);self.assertEqual(bad['irrelevant_injection_rate'],1)
 def test_quality_epoch_forces_real_prediction_instead_of_retiming_cache(self):
  import inspect
  from context_learning import shared
  from context_learning.local_cycle import _atomic_json
  self.assertIn('cache_epoch',inspect.signature(shared._predict).parameters,'quality refresh reuses old prediction')
  artifact=self.root/'head.artifact';artifact.write_bytes(b'head');artifact.chmod(0o600);calls=[]
  cases=[{'request_id':'a','input_sha256':'sha','project_key':'p','selected_ids':[],'prompt':'synthetic','candidates':[{'id':'card'}]}]
  class Trainer:
   def call(inner,mode,request,path,**kw):
    calls.append(mode);result={'predictions':[{'request_id':'a','input_sha256':'sha','selected_ids':[]}]};_atomic_json(path,result);return result,{}
  for epoch in ('first','first','fresh'):
   shared._predict(Trainer(),self.root,artifact,cases,'quality',cache_epoch=epoch)
  self.assertEqual(calls,['predict','predict'])

if __name__=='__main__':unittest.main()
