"""Disposable general project grants and corpus-independent cohort checks."""
import importlib.util
assert importlib.util.find_spec('context_learning.project_scope') is not None, 'general project authorization implementation missing'
import hashlib,json,os,shutil,sqlite3,tempfile,time
from pathlib import Path
from unittest.mock import patch
from context_learning import project_scope as scope, runtime_authorization as runtime
from context_learning import automatic_cohort,automatic_staging,learning_profile
from context_learning.contracts import digest,request
from context_learning.store import canonical
from context_learning.laya_adapter import MODEL_FILES,EDIT_INPUT_VIEW
from context_learning.shared import _db_at


def save(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(canonical(value));path.chmod(0o600)

def expect(reason, fn):
 try:fn()
 except ValueError as exc:assert reason in str(exc),(reason,str(exc))
 else:raise AssertionError('accepted '+reason)

with tempfile.TemporaryDirectory(prefix='qmd-general-scope-',dir='/tmp') as folder:
 base=Path(folder).resolve();project=base/'project';project.mkdir(mode=0o700)
 root=base/'private';root.mkdir(mode=0o700);home=base/'home';home.mkdir(mode=0o700)
 model=base/'model';model.mkdir()
 for name in MODEL_FILES:
  q=model/name;q.parent.mkdir(parents=True,exist_ok=True);q.write_text(name)
 save(model/'rl_agent_config.json',{'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
 python=base/'python';python.write_text('runtime');python.chmod(0o700)
 artifact=base/'base.artifact';artifact.write_text('base')
 installs={}
 version='0.1-test'
 for host in ('claude','codex'):
  p=home/('.'+host)/'plugins/cache/qmd-auto-context-marketplace/qmd-auto-context'/version
  p.mkdir(parents=True);shutil.copytree(Path(scope.__file__).parents[1],p/'core',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copytree(Path(scope.__file__).parents[2]/'hooks',p/'hooks')
  save(p/('.'+host+'-plugin')/'plugin.json',{'name':'qmd-auto-context','version':version})
  installs[host]=[p]
 adapter=installs['codex'][0]/'core/context_learning/laya_adapter.py'
 policy={'trainerArgv':[str(python),str(adapter),str(model)],'modelInputView':EDIT_INPUT_VIEW,'baseArtifact':str(artifact),'batchMin':5,'batchMax':100}
 save(root/'shared-policy.json',policy)
 settings={'indexing':True,'collectionPaths':{'docs':'docs'},'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':True}}
 save(project/'.auto-context/settings.json',settings)
 cutoff=int(time.time()*1000)-1000;expiry=cutoff+3600000
 with patch.object(Path,'home',return_value=home),patch.object(scope,'_installations',return_value=[{'host':host,'root':str(installs[host][0])} for host in ('claude','codex')]):
  row=scope.proposal(project,root,policy,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  assert row['scope']=='single-project-local-learning'
  assert row['cohort']['minTrainCases']==5 and row['cohort']['minQualityTrainCases']==100
  assert row['cohort']['minValidationCases']==50 and row['cohort']['minEmptyValidation']==10
  expect('acceptance',lambda:scope.activate(project,root,policy,accepted_sha256='0'*64,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff))
  answer=scope.activate(project,root,policy,accepted_sha256=scope.proposal_sha256(row),expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  assert scope.owner(root)==project and scope.present(root)
  assert scope.require(root,policy,'local_train')==row
  assert runtime.require_operation(root,policy,'local_train')==row
  assert runtime.active(root,policy,adapter,reviewed_adapter_sha256='obsolete')==row
  expect('consent',lambda:scope.require(root,policy,'gold'))
  changed={**settings,'contextLearning':{'capture':True,'autoCycle':True}}
  save(project/'.auto-context/settings.json',changed)
  assert scope.require(root,policy,'local_train')==row
  save(project/'.auto-context/settings.json',{**changed,'collectionPaths':{'docs':'.'}})
  expect('changed',lambda:scope.require(root,policy,'local_train'))
  save(project/'.auto-context/settings.json',settings)
  artifact.write_text('tampered');expect('changed',lambda:scope.require(root,policy,'local_train'));artifact.write_text('base')
  adapter.write_text('tampered');expect('changed',lambda:scope.require(root,policy,'local_train'))
  shutil.copyfile(Path(scope.__file__).with_name('laya_adapter.py'),adapter)
  with patch('context_learning.project_scope.time.time',return_value=expiry/1000+1):expect('expired',lambda:scope.require(root,policy,'local_train'))
  expect('operation',lambda:scope.require(root,policy,'apply'))
  # Official enablement changes revoke a grant even if old code remains.
  save(home/'.claude/settings.json',{'enabledPlugins':{'qmd-auto-context@qmd-auto-context-marketplace':False}})
  expect('changed',lambda:scope.require(root,policy,'local_train'))
  (home/'.claude/settings.json').unlink()
  with patch.object(scope,'_installations',return_value=[]):
   expect('changed',lambda:scope.require(root,policy,'local_train',fresh_inventory=True))
  # An edited threshold is never a fresh execution approval.
  path=root/scope.NAME;original=json.loads(path.read_text())
  lowered=json.loads(path.read_text());lowered['proposal']['cohort']['minQualityTrainCases']=1
  lowered['acceptedSha256']=scope.proposal_sha256(lowered['proposal']);save(path,lowered)
  expect('invalid',lambda:scope.require(root,policy,'local_train'));save(path,original)
  # Explicit general enrollment prepares the final settings flip, defaults preserve false.
  save(root/'live-selector.json',{'prepared':True})
  with patch('context_learning.shared._policy',return_value=policy):
   plan=learning_profile.proposal(project,root,choice='local-auto-apply',mode='preserve',expires_at_unix_ms=expiry,enable_learning=True)
   assert plan['localTrain'] and plan['autoApply'] and plan['minFreshTrainCases']==5
   assert plan['settings']['contextLearning']['capture'] is True
   assert plan['settings']['contextLearning']['autoCycle'] is True
   preserved=learning_profile.proposal(project,root,choice='local-auto-apply',mode='preserve',expires_at_unix_ms=expiry)
   assert preserved['settings']['contextLearning']['capture'] is False
  # A process reuses only an unchanged fully hashed model generation.
  from context_learning.laya_adapter import model_identity as full_identity
  with patch.object(scope,'model_identity',wraps=full_identity) as hashed:
   scope.require(root,policy,'local_train');count=hashed.call_count
   scope.require(root,policy,'local_train');assert hashed.call_count==count
  weight=model/'model.safetensors';original_weight=weight.read_bytes()
  weight.write_text('changed generation')
  expect('model_changed',lambda:scope.require(root,policy,'local_train'))
  weight.write_bytes(original_weight)
  save(root/scope.NAME,original) # Fresh receipt identity forces complete revalidation.
  assert scope.require(root,policy,'local_train')==row
  # Scope checks must reject a symlinked grant, never silently use legacy auth.
  grant=root/scope.NAME;raw=grant.read_bytes();grant.unlink();target=base/'grant';target.write_bytes(raw);target.chmod(0o600);grant.symlink_to(target)
  expect('unsafe',lambda:scope.owner(root));grant.unlink();grant.write_bytes(raw);grant.chmod(0o600)
 # General cohort: deterministic fixed source-family role, all candidates contribute exposure.
 grant=row
 with _db_at(root/'shared-learning.sqlite3') as db:
  def insert(rid,doc,body,prompt,at=None,status='gold'):
   sample=request({'prompt':prompt},[{'candidate_id':doc,'revision_sha256':digest(body),'excerpt':body,'eligible':True}],host='codex',request_id=rid)
   encoded=canonical(sample);gold={'input_sha256':digest(encoded),'selected_ids':[]}
   db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,status,split,gold,captured_at,corpus_fingerprint,use_state,family_keys) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('key',str(project),rid,digest(encoded),encoded,canonical({doc:body}),status,'train',canonical(gold),at or time.time(),'corpus','active','[]'))
  # Find cases across both deterministic roles, independent of request IDs.
  from context_learning import general_cohort
  examples={}
  for i in range(100):
   doc=f'docs/{i}.md';body=f'Unique body {i}';role,family=general_cohort.assignment([doc])
   examples.setdefault(role,(doc,body))
  assert set(examples)=={'train','holdout'}
  for role,(doc,body) in examples.items():insert(role,doc,body,role+' question')
  admitted=automatic_cohort.admitted(db,project,grant,statuses=('gold',))
  assert {role for role,_ in admitted}=={'train','holdout'}
  a,ab=examples['train'];b,bb=examples['holdout']
  sample=request({'prompt':'bridge question'},[{'candidate_id':doc,'revision_sha256':digest(body),'excerpt':body,'eligible':True} for doc,body in [(a,ab),(b,bb)]],host='codex',request_id='bridge')
  encoded=canonical(sample)
  db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,bodies,status,split,gold,captured_at,corpus_fingerprint,use_state,family_keys) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('key',str(project),'bridge',digest(encoded),encoded,canonical({a:ab,b:bb}),'failed','train',None,time.time(),'corpus','revoked','[]'))
  automatic_cohort.admitted(db,project,grant,statuses=('gold',))
  assert db.execute("SELECT role FROM project_cohort_assignments WHERE request_id='bridge'").fetchone()==('blocked',)
  # Cross-split repeated questions must exclude the train sample, preserve holdout.
  d,b=examples['train'];insert('leak',d+'x',b+'x','holdout question')
  assert dict((r[1],role) for role,r in automatic_cohort.admitted(db,project,grant,statuses=('gold',)))['leak']=='holdout'
  # Reusing a holdout document in mixed candidates may not move it into training.
  # Historical exposed bodies/questions are excluded from the new cohort.
  insert('historical','docs/old.md','Historical body','old question',at=0.1)
  insert('copied','docs/new.md','Historical body','new question')
  assert 'copied' not in {r[1] for _,r in automatic_cohort.admitted(db,project,grant,statuses=('gold',))}
  original_role=dict((r[1],role) for role,r in automatic_cohort.admitted(db,project,grant,statuses=('gold',)))['holdout']
  db.execute("DELETE FROM cases WHERE request_id='holdout'")
  d,b=examples['holdout'];insert('replacement',d,b,'different question')
  assert dict((r[1],role) for role,r in automatic_cohort.admitted(db,project,grant,statuses=('gold',)))['replacement']==original_role
  db.commit();db.execute('PRAGMA query_only=ON')
  assert automatic_cohort.admitted(db,project,grant,statuses=('gold',))
  db.execute('PRAGMA query_only=OFF')
  # A backfilled historical body revokes previously assigned fresh evidence.
  d,b=examples['train'];insert('late-history','docs/archived.md',b,'ancient question',at=.2)
  assert 'train' not in {r[1] for _,r in automatic_cohort.admitted(db,project,grant,statuses=('gold',))}
 train=[];validation=[]
 for role,count,destination in [('train',100,train),('holdout',50,validation)]:
  for i in range(count):
   values=[None]*13;values[5]=canonical({'selected_ids':[] if i<10 else ['doc']});values[12]=f'{role}/{i%5}'
   destination.append(tuple(values))
 assert automatic_staging._quality_ready(train,validation,grant['cohort'])[0]
 assert not automatic_staging._quality_ready(train[:99],validation,grant['cohort'])[0]
 assert not automatic_staging._quality_ready(train,validation[:49],grant['cohort'])[0]
 assert not automatic_staging._quality_ready(train,validation[10:],grant['cohort'])[0]
print('general scope checks passed')
