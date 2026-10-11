import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('eligible returned pool observes before topN/cutoff with honest partial metadata',()=>{
 const dir=mkdtempSync(join(tmpdir(),'qmd-pool-'));
 try{
 const result=execFileSync('python3',['-c',`
import sys,json,os,sqlite3,subprocess,hashlib
from pathlib import Path
sys.path.insert(0,'core')
from context_learning.offline import review_view,save_label,approve_review,build_manifest
from context_learning.contracts import digest,validate_label
base=Path(sys.argv[1]).resolve();state=base/'state';state.mkdir(mode=0o700)
for host in ('claude','codex'):
 root=base/host;(root/'.auto-context').mkdir(parents=True);(root/'docs').mkdir()
 hits=[]
 for i in range(6):
  (root/'docs'/f'{i}.md').write_text(('OAuth evidence '+str(i))*80)
  hits.append(dict(file=f'docs/{i}.md',score=1/(i+1),snippet='STALE INDEX TEXT',line=1,title='Synthetic'))
 fixture=base/(host+'.json');fixture.write_text(json.dumps({'results':hits}))
 config={'collections':['docs'],'collectionPaths':{'docs':'docs'},'skipPaths':['4.md'],'topN':3,'minScore':0.3}
 settings=root/'.auto-context'/'settings.json'
 def run(enabled=True,raw=None,prompt="OAuth access review"):
  settings.write_text(json.dumps(dict(config,contextLearning={'capture':enabled,'stateRoot':str(state)})))
  env=dict(os.environ,QMD_ENGINE=host,QMD_RECALL_LOG='',QMD_QUERY_FIXTURE=str(fixture),QMD_QUERY_FIXTURE_RAW=str(raw) if raw else '',INDEX_PATH=str(base/'unused-index.sqlite'),PYTHONDONTWRITEBYTECODE='1')
  return subprocess.check_output(['python3','core/recall.py'],input=json.dumps({'prompt':prompt,'cwd':str(root)}).encode(),env=env)
 before=run(False);assert run()==before
 ns=state/digest(str(root))
 def latest():
  with sqlite3.connect(ns/'learning.sqlite3') as db:return json.loads(db.execute('select body from samples order by rowid desc limit 1').fetchone()[0])
 sample=latest();meta=sample['sampling']
 assert sample['schema_version']==2 and len(sample['candidates'])==5
 assert len(meta['baseline_selected_ids'])==3 and 'docs/5.md' in meta['eligible_ids']
 assert meta['excluded']==[{'candidate_id':'docs/4.md','phase':'primary','reason':'skip'}]
 assert meta['complete_within_returned_bound'] and meta['index_revision']=='unavailable'
 assert all(c['excerpt']['truncated'] for c in sample['candidates'])
 assert all('STALE' not in c['excerpt']['text'] for c in sample['candidates'])
 cid='docs/5.md';view=review_view(ns,sample['request_id'],cid)
 negative=dict(schema_version=2,request_id=sample['request_id'],candidate_id=cid,revision_sha256=view['candidate']['revision_sha256'],abstain=False,relevance='irrelevant',evidence='',rationale={'kind':'absence','text':'Required policy details are absent from this bounded excerpt.','span':None,'scope':'provided-excerpt'})
 assert save_label(ns,negative)=='provisional'
 assert review_view(ns,sample['request_id'],cid)['status']=='provisional'
 try:approve_review(ns,negative,input_sha256='wrong',reviewer_id='synthetic',reference='synthetic://review')
 except ValueError:pass
 else:raise AssertionError('stale review accepted')
 assert approve_review(ns,negative,input_sha256=view['input_sha256'],reviewer_id='synthetic',reference='synthetic://review')=='reviewed'
 approved=review_view(ns,sample['request_id'],cid);assert approved['status']=='reviewer-approved' and approved['authentication']=='not-verified'
 assignments={sample['request_id']:{'split':'train','task_family':'oauth','document_families':{cid:'oauth-doc'}}}
 manifest=build_manifest(ns,'pool-v1',assignments,{cid:view['candidate']['revision_sha256']})
 assert manifest['sampling']=='eligible-returned-pool' and manifest['cases'][0]['class_index']==2
 # Over-return, missing and oversize snapshots are visible rather than full-pool claims.
 fixture.write_text(json.dumps({'results':hits+[dict(hits[0],file='docs/missing.md'),dict(hits[0],file='docs/large.md'),dict(hits[0],file='docs/extra.md')]}))
 (root/'docs'/'large.md').write_bytes(b'x'*65537)
 assert run(False)==run()
 partial=latest()['sampling'];assert partial['retrieval_truncated'] and len(partial['snapshot_failures'])==2 and not partial['complete_within_returned_bound']
 with sqlite3.connect(ns/'learning.sqlite3') as db:db.execute('PRAGMA user_version=99')
 assert run()==run(False)
 with sqlite3.connect(ns/'learning.sqlite3') as db:db.execute('PRAGMA user_version=1')
 # Existing hierarchical fixture path: untrusted wiki primary falls back to raw.
 config.update(collections=['wiki','docs'],collectionPaths={'wiki':'.auto-context/wiki','docs':'docs'},collectionRoles={'wiki':'wiki','docs':'raw'},recallStrategy='hierarchical',minScore=0)
 (root/'.auto-context'/'wiki').mkdir()
 fixture.write_text(json.dumps({'results':[dict(file='wiki/untrusted.md',score=1)]}))
 raw=base/(host+'-raw.json');raw.write_text(json.dumps({'results':hits[:4]}))
 assert run(False,raw)==run(True,raw)
 fallback=latest()['sampling'];assert set(fallback['phase_counts'])=={'primary','raw'}
 assert fallback['excluded'][0]['candidate_id']=='wiki/untrusted.md' and len(fallback['captured_ids'])==4
 # Trusted wiki: topN stops production freshness scanning before lower candidates.
 config.update(topN=1,skipPaths=[],wikiPath='.auto-context/wiki')
 cards=[]
 for name in ('selected','joint','stale','untrusted','excluded'):
  source=root/'docs'/('source-'+name+'.md');source.write_text('Synthetic source '+name)
  st=source.stat();sha=hashlib.sha256(source.read_bytes()).hexdigest()
  status='contested' if name=='excluded' else 'verified'
  creator='foreign' if name=='untrusted' else 'qmd-auto-context'
  body='Grant requires approval.' if name=='selected' else 'Audit requires retention.'
  card=root/'.auto-context'/'wiki'/(name+'.md')
  rev=json.dumps({'kind':'file','path':'docs/'+source.name,'collection':'docs','sha256':sha,'size':st.st_size,'mtimeNs':st.st_mtime_ns})
  card.write_text('---'+chr(10)+'title: Synthetic'+chr(10)+'status: '+status+chr(10)+'createdBy: '+creator+chr(10)+'sourceRevisions:'+chr(10)+'  - '+rev+chr(10)+'---'+chr(10)+body)
  cards.append(dict(file='wiki/'+name+'.md',score=1-len(cards)/10))
  if name=='stale':source.write_text('Changed after compiler provenance')
 fixture.write_text(json.dumps({'results':cards}))
 assert run(False,raw)==run(True,raw)
 joint=latest();sampling=joint['sampling']
 assert sampling['baseline_selected_ids']==['wiki/selected.md']
 assert sampling['captured_ids']==['wiki/selected.md','wiki/joint.md']
 reasons={x['candidate_id']:x['reason'] for x in sampling['excluded']}
 assert reasons['wiki/stale.md']=='freshness_stale' and reasons['wiki/untrusted.md']=='unverified' and reasons['wiki/excluded.md']=='excluded'
 assert sampling['complete_within_returned_bound']
 families={};revisions={}
 for cid,quote in [('wiki/selected.md','Grant requires approval.'),('wiki/joint.md','Audit requires retention.')]:
  view=review_view(ns,joint['request_id'],cid);text=view['candidate']['excerpt']['text'];start=text.index(quote)
  label=dict(schema_version=2,request_id=joint['request_id'],candidate_id=cid,revision_sha256=view['candidate']['revision_sha256'],abstain=False,relevance='necessary',evidence=quote,rationale={'kind':'support','text':'This is one distinct requirement needed alongside the other candidate.','span':{'start':start,'end':start+len(quote)},'scope':'provided-excerpt'})
  approve_review(ns,label,input_sha256=view['input_sha256'],reviewer_id='synthetic',reference='synthetic://joint')
  assert review_view(ns,joint['request_id'],cid)['label']['relevance']=='necessary'
  families[cid]='joint-policy';revisions[cid]=view['candidate']['revision_sha256']
 both=build_manifest(ns,'joint-v1',{joint['request_id']:{'split':'train','task_family':'joint-policy','document_families':families}},revisions)
 assert len(both['cases'])==2 and all(c['class_index']==0 for c in both['cases'])
 # A contradiction uses a real quote/span; invalid span is rejected.
 config.update(collections=['docs'],collectionPaths={'docs':'docs'},collectionRoles={'docs':'raw'},recallStrategy='flat')
 quote='OAuth access must be denied.'
 (root/'docs'/'contradiction.md').write_text(quote)
 fixture.write_text(json.dumps({'results':[dict(file='docs/contradiction.md',score=1)]}))
 prompt='Find evidence that OAuth access is permitted.'
 assert run(False,prompt=prompt)==run(True,prompt=prompt)
 contrary_sample=latest();view=review_view(ns,contrary_sample['request_id'],'docs/contradiction.md');text=view['candidate']['excerpt']['text'];start=text.index(quote)
 contrary=dict(schema_version=2,request_id=contrary_sample['request_id'],candidate_id='docs/contradiction.md',revision_sha256=view['candidate']['revision_sha256'],abstain=False,relevance='irrelevant',evidence=quote,rationale={'kind':'contradiction','text':'The explicit denial contradicts the requested permission claim.','span':{'start':start,'end':start+len(quote)},'scope':'provided-excerpt'})
 bad=dict(contrary,rationale=dict(contrary['rationale'],span={'start':1,'end':1+len(quote)}))
 try:validate_label(bad,contrary_sample)
 except ValueError:pass
 else:raise AssertionError('wrong contradiction span accepted')
 approve_review(ns,contrary,input_sha256=view['input_sha256'],reviewer_id='synthetic',reference='synthetic://contradiction')
 assert review_view(ns,contrary_sample['request_id'],'docs/contradiction.md')['label']['rationale']['kind']=='contradiction'
print('ok')
`,dir],{encoding:'utf8',timeout:20000,env:{...process.env,QMD_RECALL_LOG:'',QMD_LIVE:'',PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(result.trim(),'ok');
 }finally{rmSync(dir,{recursive:true,force:true});}
});
