"""Synthetic only: no live corpus, models, providers or daemon calls."""
import contextlib,copy,hashlib,io,json,os,sqlite3,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,'core')
from context_learning.pool import expand,RAW_LIMIT
from context_learning.contracts import digest,validate_request,request
from context_learning.store import canonical
from context_learning.offline import approve_review,build_manifest
from context_learning.dataset import export_dataset
from context_learning.laya_raw import export_laya_raw
from context_learning.selection_metrics import evaluate_selection
from config import normalize_config

class Checks(unittest.TestCase):
 def phases(self,hits):return [dict(name='primary',results=hits[:8],wiki_scoped=False,returned_count=len(hits),unbounded_results=hits)]
 def expand(self,phases,**kw):
  defaults=dict(top_k=15,retrieve=lambda p,k:None,prepare=lambda h:None,
   hard_verdict=lambda h,s:h.get('hard','eligible'),fresh_verdict=lambda h,s:h.get('fresh','eligible'),
   cutoff=lambda n:0,is_wiki=lambda h:h.get('wiki',False))
  defaults.update(kw);return expand(phases,**defaults)
 def accepted(self,out):return [h['file'] for p in out[0] for h in p['results'] if out[1](h,p['wiki_scoped'])=='eligible']
 def hits(self,n):return [dict(file='docs/'+str(i)+'.md',score=1/(i+1)) for i in range(n)]
 def test_bounded_eligible_dedup_reuse_and_failure(self):
  hits=self.hits(50);hits[0]['hard']='unverified';hits[1]['fresh']='freshness_stale';hits.insert(3,copy.deepcopy(hits[2]))
  before=copy.deepcopy(hits);calls=[]
  with patch('context_learning.pool.deepcopy', wraps=copy.deepcopy) as clone:
   result=self.expand(self.phases(hits),prepare=lambda h:calls.append(len(h)))
   self.assertEqual(len(clone.call_args.args[0]),30)
  self.assertEqual(calls,[30]);self.assertEqual(len(self.accepted(result)),15)
  self.assertEqual(len(set(self.accepted(result))),15);self.assertEqual(hits,before)
  self.assertNotIn('docs/0.md',self.accepted(result));self.assertNotIn('docs/1.md',self.accepted(result))
  self.assertEqual(result[0][0]['returned_count'],51)
  self.assertEqual(len(self.accepted(self.expand(self.phases(self.hits(4))))),4)
  phases=self.phases(self.hits(8));phases[0].pop('unbounded_results');calls=[]
  self.assertEqual(len(self.accepted(self.expand(phases,retrieve=lambda p,k:calls.append(k) or self.hits(18)))),15)
  self.assertEqual(calls,[30])
  with self.assertRaisesRegex(ValueError,'expanded_query_failed'):self.expand(phases)
  phases=self.phases(self.hits(4));phases[0].pop('unbounded_results')
  self.assertEqual(len(self.accepted(self.expand(phases,retrieve=lambda p,k:(_ for _ in ()).throw(AssertionError('duplicate query'))))),4)
 def test_cutoff_rescue_freshness_and_wiki_raw_policy(self):
  hits=self.hits(18)
  self.assertEqual(len(self.accepted(self.expand(self.phases(hits),cutoff=lambda n:.3))),3)
  self.assertEqual(self.accepted(self.expand(self.phases(hits),cutoff=lambda n:2)),[])
  hits[0]['hard']='unverified'
  rescued=self.expand(self.phases(hits),cutoff=lambda n:.9)
  self.assertEqual(self.accepted(rescued),['docs/1.md'])
  hits[1]['fresh']='freshness_stale'
  self.assertEqual(self.accepted(self.expand(self.phases(hits),cutoff=lambda n:.9)),[])
  wiki=dict(file='wiki/a.md',score=.4,wiki=True)
  mixed=self.phases([dict(file='docs/a.md',score=1),wiki])
  self.assertEqual(self.accepted(self.expand(mixed,hierarchical=True)),['wiki/a.md'])
  phases=self.phases([dict(wiki,fresh='freshness_stale')]);phases.append(dict(name='raw',wiki_scoped=False,results=self.hits(3),returned_count=3))
  self.assertEqual(len(self.accepted(self.expand(phases,hierarchical=True))),3)
  phases[0]=self.phases([wiki])[0]
  self.assertEqual(self.accepted(self.expand(phases,hierarchical=True)),['wiki/a.md'])
 def test_defaults_and_actual_selection_metrics(self):
  for value in (None,True,0,16,'15'):
   self.assertEqual(normalize_config({'contextLearning':{'candidateTopK':value}})['contextLearning']['candidateTopK'],8)
  self.assertEqual(normalize_config({'contextLearning':{'candidateTopK':15}})['contextLearning']['candidateTopK'],15)
  kw=dict(reference_ids=['a','b','c'],necessary_ids=['a','b'],pool_ids=['a','c'],final_ids=['a'],fallback=True)
  report=evaluate_selection(**kw)
  self.assertEqual(report['conditional_actual_omission_rate'],0);self.assertEqual(report['end_to_end_reference_omission_rate'],.5)
  self.assertEqual(report['fallback'],True)
  self.assertIsNone(evaluate_selection(reference_ids=[],necessary_ids=[],pool_ids=[],final_ids=[],fallback=False)['conditional_actual_omission_rate'])
 def test_mock_http_search_limits_failure_and_response_byte_bound(self):
  import recall
  with tempfile.TemporaryDirectory(prefix='qmd-top15-http-') as tmp:
   root=Path(tmp).resolve();project=root/'project';(project/'.auto-context').mkdir(parents=True);(project/'docs').mkdir()
   state=root/'private';state.mkdir(mode=0o700);hits=self.hits(35)
   for i in range(35):(project/'docs'/(str(i)+'.md')).write_text('Synthetic HTTP mock evidence '+str(i))
   calls=[];reads=[];timeouts=[]
   class Response:
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,size=-1):reads.append(size);return self.body if size<0 else self.body[:size]
   def run(k,mode='ok'):
    (project/'.auto-context'/'settings.json').write_text(json.dumps(dict(collections=['docs'],collectionPaths={'docs':'docs'},recallStrategy='flat',contextLearning=dict(capture=True,stateRoot=str(state),candidateTopK=k))))
    def urlopen(req,timeout):
     data=json.loads(req.data);limit=data['limit'];calls.append(limit);timeouts.append(timeout)
     if limit==30 and mode=='failure':raise recall.urllib.error.URLError('synthetic failure')
     body=b'x'*262145 if limit==30 and mode=='oversize' else json.dumps(dict(results=hits[:8] if limit==8 else hits)).encode()
     return Response(body)
    output=io.StringIO()
    with patch.dict(os.environ,dict(QMD_ENGINE='codex',QMD_QUERY_FIXTURE='',QMD_QUERY_FIXTURE_RAW='',QMD_RECALL_LOG='',QMD_SANDBOX='',QMD_SETUP_GUARD_FIXTURE='1',INDEX_PATH=str(root/'unused-index.sqlite')),clear=False),patch.object(recall,'daemon_alive',return_value=True),patch.object(recall,'narrow_general_lex',return_value=dict(mode='not_needed',absent=0,absent_in_cut=0,identifier_present=False)),patch.object(recall.urllib.request,'urlopen',side_effect=urlopen),patch.object(sys,'stdin',io.StringIO(json.dumps(dict(prompt='Review synthetic HTTP OAuth evidence',cwd=str(project))))),contextlib.redirect_stdout(output):
     recall.main()
    return output.getvalue()
   baseline=run(8);self.assertEqual(calls,[8]);calls.clear();reads.clear();timeouts.clear()
   self.assertEqual(run(15),baseline);self.assertEqual(calls,[8,30]);self.assertEqual(reads,[-1,262145]);self.assertLessEqual(timeouts[-1],1)
   for mode in ('failure','oversize'):
    calls.clear();self.assertEqual(run(15,mode),baseline);self.assertEqual(calls,[8,30])
   ns=state/digest(str(project))
   with contextlib.closing(sqlite3.connect(ns/'learning.sqlite3')) as db:sample=json.loads(db.execute('select body from samples order by rowid desc limit 1').fetchone()[0])
   self.assertEqual(sample['schema_version'],2)
 def test_hook_equivalence_schema_export_and_bounded_snapshots(self):
  with tempfile.TemporaryDirectory(prefix='qmd-top15-') as tmp:
   base=Path(tmp).resolve();state=base/'private';state.mkdir(mode=0o700)
   for host in ('claude','codex','hermes'):
    project=base/host;(project/'.auto-context').mkdir(parents=True);(project/'docs').mkdir()
    hits=self.hits(36)
    for i in range(36):(project/'docs'/(str(i)+'.md')).write_text('Synthetic approval evidence '+str(i))
    fixture=base/(host+'.json');fixture.write_text(json.dumps({'results':hits}))
    raw_fixture=None
    cfg=dict(collections=['docs'],collectionPaths={'docs':'docs'},topN=3,minScore=0,recallStrategy='flat')
    def run(k=None,enabled=True,failed=False):
     cfg['contextLearning']=dict(capture=enabled,stateRoot=str(state),candidateTopK=k)
     (project/'.auto-context'/'settings.json').write_text(json.dumps(cfg))
     env=dict(os.environ,QMD_ENGINE=host,QMD_QUERY_FIXTURE=str(fixture),QMD_RECALL_LOG='',QMD_QUERY_FIXTURE_RAW=str(raw_fixture) if raw_fixture else '',QMD_SETUP_GUARD_FIXTURE='1',INDEX_PATH=str(base/'unused-index.sqlite'),PYTHONDONTWRITEBYTECODE='1')
     command=[sys.executable,'core/recall.py'] if not failed else [sys.executable,'-c',"import sys;sys.path.insert(0,'core');from unittest.mock import patch;import recall;patch('context_learning.pool.expand',side_effect=RuntimeError('synthetic failure')).start();recall.main()"]
     return subprocess.check_output(command,input=json.dumps(dict(cwd=str(project),prompt='Review synthetic OAuth approval evidence')).encode(),env=env,timeout=3)
    baseline=run(enabled=False);self.assertEqual(run(),baseline);self.assertEqual(run(15),baseline)
    ns=state/digest(str(project))
    def latest():
     with contextlib.closing(sqlite3.connect(ns/'learning.sqlite3')) as db:return json.loads(db.execute('select body from samples order by rowid desc limit 1').fetchone()[0])
    self.assertEqual(run(15,failed=True),baseline);self.assertEqual(latest()['schema_version'],2)
    self.assertEqual(run(15),baseline)
    sample=latest();validate_request(sample)
    self.assertEqual(sample['schema_version'],3);self.assertEqual(len(sample['candidates']),15)
    self.assertEqual(sample['sampling']['per_phase_limit'],30);self.assertEqual(sample['sampling']['candidate_limit'],15)
    self.assertEqual(sample['sampling']['baseline_selected_ids'],['docs/0.md','docs/1.md','docs/2.md'])
    self.assertTrue(sample['sampling']['retrieval_truncated']);self.assertFalse(sample['sampling']['complete_within_returned_bound'])
    # v3 uses unchanged reviewed label v2 and neutral export provenance.
    c=sample['candidates'][0];text=c['excerpt']['text'];cid=c['candidate_id']
    label=dict(schema_version=2,request_id=sample['request_id'],candidate_id=cid,revision_sha256=c['revision_sha256'],abstain=False,relevance='necessary',evidence=text,rationale=dict(kind='support',text='Synthetic reviewed evidence.',span=dict(start=0,end=len(text)),scope='provided-excerpt'))
    approve_review(ns,label,input_sha256=digest(canonical(sample)),reviewer_id='synthetic',reference='synthetic://top15')
    revisions={cid:c['revision_sha256']};build_manifest(ns,'top15',{sample['request_id']:dict(split='train',task_family='synthetic',document_families={cid:'synthetic-doc'})},revisions)
    export=export_dataset(ns,'top15',revisions)
    row=json.loads((Path(export['directory'])/'train.jsonl').read_text());self.assertEqual(row['provenance']['sample_schema_version'],3)
    self.assertEqual(export_laya_raw(ns,'top15',revisions)['counts']['train'],1)
    # Fewer available, skips/duplicates and bounded snapshot failure stay honest.
    fixture.write_text(json.dumps({'results':hits[:4]}));self.assertEqual(run(15),run(enabled=False));self.assertEqual(len(latest()['candidates']),4)
    fixture.write_text(json.dumps({'results':hits[:18]+[hits[2]]}));cfg['skipPaths']=['3.md'];(project/'docs'/'4.md').write_bytes(b'x'*65537)
    self.assertEqual(run(15),run(enabled=False));sample=latest()
    self.assertNotIn('docs/3.md',sample['sampling']['captured_ids']);self.assertTrue(sample['sampling']['snapshot_failures'])
    (project/'docs'/'4.md').write_text('Synthetic restored bounded evidence.')
    # Real annotation/freshness path: wiki beyond baseline 8 may be observed,
    # but cannot cancel baseline raw fallback or change its actual output.
    cfg.update(collections=['wiki','docs'],collectionPaths={'wiki':'.auto-context/wiki','docs':'docs'},collectionRoles={'wiki':'wiki','docs':'raw'},recallStrategy='hierarchical',wikiPath='.auto-context/wiki',skipPaths=[])
    wiki=project/'.auto-context'/'wiki';wiki.mkdir();cards=[]
    for i in range(18):
     source=project/'docs'/('source-'+str(i)+'.md');source.write_text('Synthetic source '+str(i));st=source.stat()
     revision=json.dumps(dict(kind='file',path='docs/'+source.name,collection='docs',sha256=hashlib.sha256(source.read_bytes()).hexdigest(),size=st.st_size,mtimeNs=st.st_mtime_ns))
     card=wiki/(str(i)+'.md');card.write_text('---'+chr(10)+'title: Synthetic'+chr(10)+'status: verified'+chr(10)+'createdBy: qmd-auto-context'+chr(10)+'sourceRevisions:'+chr(10)+'  - '+revision+chr(10)+'---'+chr(10)+'Synthetic approval evidence.')
     if i<8:source.write_text('Changed after provenance')
     cards.append(dict(file='wiki/'+str(i)+'.md',score=1/(i+1)))
    fixture.write_text(json.dumps({'results':cards}));raw_fixture=base/(host+'-raw.json');raw_fixture.write_text(json.dumps({'results':hits[:18]}))
    baseline_raw=run(enabled=False);self.assertIn(b'docs/0.md',baseline_raw);self.assertEqual(run(15),baseline_raw)
    sample=latest();self.assertEqual(sample['schema_version'],3)
    self.assertEqual(set(sample['sampling']['phase_counts']),{'primary','raw'})
    self.assertEqual(sample['sampling']['captured_ids'],['wiki/'+str(i)+'.md' for i in range(8,18)])
    self.assertEqual(sample['sampling']['baseline_selected_ids'],['docs/0.md','docs/1.md','docs/2.md'])
    reasons={x['reason'] for x in sample['sampling']['excluded']};self.assertIn('freshness_stale',reasons);self.assertIn('wiki_priority',reasons)
    # No fresh wiki anywhere: bounded raw observation may reach15, unchanged output.
    fixture.write_text(json.dumps({'results':cards[:8]}));self.assertEqual(run(15),run(enabled=False))
    sample=latest();self.assertEqual(len(sample['candidates']),15);self.assertTrue(all(c['candidate_id'].startswith('docs/') for c in sample['candidates']))
    # Capture DB failure cannot alter hook bytes.
    with contextlib.closing(sqlite3.connect(ns/'learning.sqlite3')) as db:db.execute('PRAGMA user_version=99')
    self.assertEqual(run(15),run(enabled=False))

if __name__=='__main__':unittest.main()
