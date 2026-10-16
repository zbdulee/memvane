"""Synthetic durable batch lifecycle, no model or teacher imports."""
from contextlib import contextmanager
import json, os
from pathlib import Path
import sqlite3,tempfile,time,unittest
from context_learning.learning_queue import LearningQueue, semantic_key

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
  @contextmanager
  def connect():
   db=sqlite3.connect(self.root/'fixture.sqlite3',timeout=.1)
   try:
    with db:yield db
   finally:db.close()
  self.q=LearningQueue(self.root,connect=connect)
 def row(self,i,rid=None):
  return ('owner',rid or f'r{i}','input'+str(i),json.dumps({'prompt':{'text':f'question {i}'},'candidates':[{'candidate_id':'card','revision_sha256':str(i)}],'request_id':rid or f'r{i}'}),json.dumps({'card':f'body {i}'}),json.dumps({'selected_ids':['card'],'input_sha256':'changes-with-uuid'}),'train','/synthetic/owner','corpus','[]',None,100,'source')
 def plan(self,rows,**kw):return self.q.plan(rows,approval='a'*64,cohort='c',family='f'*64,minimum=2,maximum=4,**kw)
 def test_semantic_identity_ignores_uuid_time_but_fences_revision_label_scope_model(self):
  a=self.row(1);b=list(self.row(1,'new-uuid'));b[11]=200
  self.assertEqual(semantic_key(a,'ns','family'),semantic_key(b,'ns','family'))
  for index,value in [(4,json.dumps({'card':'new body'})),(5,json.dumps({'selected_ids':[]})),(7,'/other')]:
   c=list(a);c[index]=value;self.assertNotEqual(semantic_key(a,'ns','family'),semantic_key(c,'ns','family'))
  self.assertNotEqual(semantic_key(a,'ns','family'),semantic_key(a,'ns','other'))
 def test_duplicate_wakes_and_completed_evidence_not_consumed_twice(self):
  rows=[self.row(i) for i in range(2)];job=self.plan(rows)
  self.assertEqual(job,self.plan(rows))
  with self.q.claim(job['id']) as claim:
   self.assertIsNotNone(claim)
   with self.q.claim(job['id']) as duplicate:self.assertIsNone(duplicate)
   self.q.complete(job['id'])
  renamed=[self.row(i,'new'+str(i)) for i in range(2)]
  again=self.plan(renamed);self.assertEqual(again['id'],job['id']);self.assertEqual(again['status'],'staged')
  with self.q.claim(job['id']) as duplicate:self.assertIsNone(duplicate)
 def test_fresh_threshold_bounded_replay_and_holdout_rejected(self):
  rows=[self.row(i) for i in range(10)];job=self.plan(rows)
  self.assertLessEqual(len(job['members']),4)
  with self.q.claim(job['id']):self.q.complete(job['id'])
  nextjob=self.plan(rows);self.assertNotEqual(nextjob['id'],job['id']);self.assertLessEqual(len(nextjob['members']),4)
  hold=list(self.row(12));hold[6]='holdout'
  with self.assertRaises(ValueError):self.plan([hold,self.row(11)])
 def test_frozen_cohort_role_overrides_provisional_split(self):
  rows=[list(self.row(i)) for i in range(2)]
  for r in rows:r[6]='holdout'
  job=self.q.plan(rows,approval='a'*64,cohort='c',family='f'*64,minimum=2,maximum=4,roles=['train','train'])
  self.assertEqual(len(job['members']),2)
  with self.assertRaises(ValueError):self.q.plan(rows,approval='a'*64,cohort='c',family='f'*64,minimum=2,maximum=4,roles=['holdout','train'])
 def test_abandoned_claim_recovers_same_job_without_new_reservation(self):
  job=self.plan([self.row(1),self.row(2)])
  with self.q.claim(job['id']) as claim:self.assertIsNotNone(claim)
  with self.q.connect() as db:db.execute("UPDATE learning_jobs SET status='running',retry_at=0 WHERE job_id=?",(job['id'],))
  with self.q.claim(job['id']) as recovered:self.assertIsNotNone(recovered)
  self.assertEqual(self.plan([self.row(1),self.row(2)])['id'],job['id'])
 def test_resource_backoff_and_bounded_failure(self):
  job=self.plan([self.row(1),self.row(2)])
  self.q.defer(job['id'],'memory',now=100)
  self.assertGreater(self.q.get(job['id'])['retry_at'],100)
  with self.q.claim(job['id'],now=100) as claim:self.assertIsNone(claim)
  for _ in range(3):self.q.defer(job['id'],'failed',failure=True,now=100)
  self.assertEqual(self.q.get(job['id'])['status'],'blocked')
 def test_cumulative_component_merge_does_not_reconsume_deleted_capture_evidence(self):
  options=dict(approval='a'*64,cohort='general',family='f'*64,minimum=5,maximum=128,cumulative=True)
  rows=[self.row(i) for i in range(5)];job=self.q.plan(rows,**options)
  with self.q.claim(job['id']):self.q.complete(job['id'])
  recaptured=[]
  for i in range(5):
   row=list(self.row(i,f'recaptured-{i}'));row[12]='train/merged-component';recaptured.append(tuple(row))
  repeated=self.q.plan(recaptured,**options)
  self.assertEqual(repeated['id'],job['id'])
  with self.q.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_jobs').fetchone()[0],1)
 def test_cumulative_query_variants_do_not_inflate_fresh_training(self):
  variants=['Question 0',' question 0 ','QUESTION 0','question   0','Question\t0']
  rows=[]
  for i,prompt in enumerate(variants):
   row=list(self.row(0,f'variant-{i}'));sample=json.loads(row[3]);sample['prompt']['text']=prompt;row[3]=json.dumps(sample);rows.append(tuple(row))
  job=self.q.plan(rows,approval='a'*64,cohort='general',family='f'*64,minimum=5,maximum=128,cumulative=True)
  self.assertIsNone(job)
 def test_cumulative_manifest_revalidates_the_same_normalized_identity(self):
  rows=[self.row(i) for i in range(5)]
  job=self.q.plan(rows,approval='a'*64,cohort='general',family='f'*64,minimum=5,maximum=128,cumulative=True)
  self.assertEqual(job.get('queryNormalization'),'casefold-whitespace-v1')
  self.assertEqual(self.q.rows(job,rows),rows)
 def test_cumulative_project_replay_can_reach_quality_without_100_fresh_at_once(self):
  options=dict(approval='a'*64,cohort='general',family='f'*64,minimum=5,maximum=128,cumulative=True)
  rows=[]
  for start in range(0,100,5):
   rows.extend(self.row(i) for i in range(start,start+5))
   job=self.q.plan(rows,**options)
   self.assertEqual(len(job['members']),len(rows))
   self.assertEqual(sum(m['fresh'] for m in job['members']),5)
   with self.q.claim(job['id']):self.q.complete(job['id'])
  self.assertEqual(len(job['members']),100)
  self.assertEqual(sum(m['fresh'] for m in job['members']),5)
 def test_inherited_child_keeps_abandoned_owner_fenced(self):
  import subprocess,sys
  job=self.plan([self.row(1),self.row(2)])
  with self.q.connect() as db:db.execute("UPDATE learning_jobs SET status='running' WHERE job_id=?",(job['id'],))
  script="import os,fcntl,subprocess,sys;fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX);subprocess.Popen([sys.executable,'-c',\"import pathlib,time,sys;pathlib.Path(sys.argv[1]).write_text('ready');time.sleep(.3)\",sys.argv[2]],pass_fds=(fd,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);os._exit(0)"
  ready=self.root/'child-ready'
  parent=subprocess.Popen([sys.executable,'-c',script,str(self.root/'learning-claim.lock'),str(ready)])
  parent.wait(timeout=2)
  deadline=time.monotonic()+2
  while not ready.exists() and time.monotonic()<deadline:time.sleep(.005)
  self.assertTrue(ready.exists())
  with self.q.claim(job['id']) as claim:self.assertIsNone(claim)
  while time.monotonic()<deadline:
   with self.q.claim(job['id']) as claim:
    if claim is not None:break
   time.sleep(.01)
  else:self.fail('inherited lease did not release')
 def test_changed_authorization_cannot_take_old_pinned_job(self):
  job=self.plan([self.row(1),self.row(2)])
  other=self.q.plan([self.row(1),self.row(2)],approval='b'*64,cohort='c',family='f'*64,minimum=2,maximum=4)
  self.assertNotEqual(job['id'],other['id'])
if __name__=='__main__':unittest.main()
