"""Reviewed labels, frozen manifests, family splits and deletion use synthetic state only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memvane.learning.context_learning.contracts import request, digest, CLASS_ORDER, validate_label
from memvane.learning.context_learning.store import capture
from memvane.learning.context_learning.offline import save_label, build_manifest, load_manifest, delete_case


def _check(root):
 def bad(fn):
  try: fn()
  except ValueError: pass
  else: raise AssertionError('expected rejection')
 def sample(rid,cid,text):
  return request({'prompt':'Question '+rid},[{'candidate_id':cid,'revision_sha256':digest(text),'excerpt':text,'eligible':True}],host='codex',request_id=rid)
 def label(s):
  c=s['candidates'][0]
  return dict(schema_version=1,request_id=s['request_id'],candidate_id=c['candidate_id'],revision_sha256=c['revision_sha256'],abstain=False,relevance='necessary',evidence=c['excerpt']['text'])
 def review(l):
  return dict(reviewer_id='synthetic-reviewer',reference='synthetic://verified-case',evidence_sha256=digest(l['evidence']))
 def assignment(split,task,cid,doc):
  return dict(split=split,task_family=task,document_families={cid:doc})
 s1=sample('r1','doc1','alpha evidence');s2=sample('r2','doc2','beta evidence')
 l1=label(s1);l2=label(s2)
 for s in (s1,s2): assert capture(s,enabled=True,state_dir=root)=='stored'
 assert capture(s1,enabled=True,state_dir=root)=='stored'
 assert capture(dict(s1,host='claude'),enabled=True,state_dir=root)=='request_conflict'
 assert save_label(root,l1)=='provisional'
 a={'r1':assignment('train','task1','doc1','family1'),'r2':assignment('evaluation','task2','doc2','family2')}
 revisions={'doc1':digest('alpha evidence'),'doc2':digest('beta evidence')}
 assert build_manifest(root,'provisional-only',a,revisions)['cases']==[]
 for change in ({'revision_sha256':digest('old')},{'evidence':'fabricated'},{'schema_version':True},{'relevance':[]}):
  bad(lambda:save_label(root,dict(l1,**change),review=review(l1)))
 bad(lambda:save_label(root,l1,review=dict(review(l1),evidence_sha256='wrong')))
 assert save_label(root,l1,review=review(l1))=='reviewed'
 assert save_label(root,l1,review=review(l1))=='reviewed'
 bad(lambda:save_label(root,dict(l1,relevance='supporting'),review=review(l1)))
 assert save_label(root,l2,review=review(l2))=='reviewed'
 m=build_manifest(root,'v1',a,revisions)
 assert m['class_order']==list(CLASS_ORDER) and len(m['cases'])==2
 assert m==build_manifest(root,'v1',a,revisions)==load_manifest(root,'v1')
 bad(lambda:build_manifest(root,'v1',{'r1':a['r1']},revisions))
 for changed in (
  {'r1':dict(a['r1'],split='evaluation'),'r2':a['r2']},
  {'r1':a['r1'],'r2':dict(a['r2'],task_family='task1')},
  {'r1':a['r1'],'r2':dict(a['r2'],document_families={'doc2':'family1'})}):
  bad(lambda:build_manifest(root,'leak',changed,revisions))
 with sqlite3.connect(root/'learning.sqlite3') as db:
  assert db.execute("select count(*) from manifests where version_id='leak'").fetchone()[0]==0
 stale=build_manifest(root,'stale',a,dict(revisions,doc1=digest('new revision')))
 assert len(stale['cases'])==1 and stale['excluded']['stale']==1
 s3=sample('r3','doc3','gamma evidence');capture(s3,enabled=True,state_dir=root)
 l3=dict(label(s3),abstain=True,relevance=None,evidence='')
 save_label(root,l3,review=review(l3))
 a3=dict(a,r3=assignment('train','task3','doc3','family3'))
 assert build_manifest(root,'abstained',a3,dict(revisions,doc3=digest('gamma evidence')))['excluded']['abstained']==1
 assert delete_case(root,'r1')=='deleted' and delete_case(root,'r1')=='deleted'
 assert capture(s1,enabled=True,state_dir=root)=='deleted_case'
 bad(lambda:save_label(root,l1,review=review(l1)))
 bad(lambda:load_manifest(root,'v1'))
 bad(lambda:build_manifest(root,'v1',a,revisions))
 derived=build_manifest(root,'replay-v2',a,revisions,parent_id='v1')
 assert len(derived['cases'])==1 and derived['cases'][0]['case_hash']==digest(json.dumps(['r2','doc2'],ensure_ascii=False,sort_keys=True,separators=(',',':')))
 assert load_manifest(root,'replay-v2')==derived
 assert load_manifest(root,'stale')==stale
 assert delete_case(root,'r2')=='deleted'
 bad(lambda:load_manifest(root,'replay-v2'))
 assert build_manifest(root,'replay-v3',a,revisions,parent_id='replay-v2')['cases']==[]
 with sqlite3.connect(root/'learning.sqlite3') as db:
  assert db.execute("select count(*) from samples where request_id='r1'").fetchone()[0]==0
  assert db.execute("select count(*) from labels where request_id='r1'").fetchone()[0]==0
  assert db.execute("select count(*) from manifest_cases where request_id='r1'").fetchone()[0]==0
  assert db.execute("select count(*) from tombstones where request_id='r1'").fetchone()[0]==1
  assert 'alpha evidence' not in db.execute("select body from manifests where version_id='v1'").fetchone()[0]
 # Same exact query or document content cannot cross splits under new IDs.
 for name in ('query-copy','content-copy'):
  isolated=root/name;isolated.mkdir(mode=0o700)
  first=sample('a','a-doc','original evidence')
  second=sample('b','b-doc','other evidence' if name=='query-copy' else 'original evidence')
  if name=='query-copy': second['prompt']=dict(first['prompt'])
  for item in (first,second):
   capture(item,enabled=True,state_dir=isolated)
   item_label=label(item);save_label(isolated,item_label,review=review(item_label))
  choices={'a':assignment('train','a-task','a-doc','a-family'),'b':assignment('evaluation','b-task','b-doc','b-family')}
  current={s['candidates'][0]['candidate_id']:s['candidates'][0]['revision_sha256'] for s in (first,second)}
  bad(lambda:build_manifest(isolated,'leak',choices,current))
 assert capture(dict(s1,schema_version=True),enabled=True,state_dir=root)=='invalid_schema'


class OfflineDataset(unittest.TestCase):
    def test_reviewed_labels_frozen_manifests_family_splits_and_deletion(self):
        with tempfile.TemporaryDirectory(prefix='qmd-offline-') as name:
            _check(Path(name).resolve())


if __name__ == '__main__':
    unittest.main()
