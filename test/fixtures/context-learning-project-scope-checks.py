"""Disposable general project grants and corpus-independent cohort checks."""
import importlib.util
assert importlib.util.find_spec('context_learning.project_scope') is not None, 'general project authorization implementation missing'
import hashlib,json,os,shutil,sqlite3,sys,tempfile,time
from collections import Counter
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
 # The accepted runtime identity must remain owner-controlled and unaliased.
 assert scope._path(python)==python
 python.chmod(0o720)
 expect('unsafe_path',lambda:scope._path(python));python.chmod(0o700)
 alias=base/'runtime-alias';alias.symlink_to(python)
 expect('unsafe_path',lambda:scope._path(alias));alias.unlink()
 os.link(python,alias)
 expect('unsafe_path',lambda:scope._path(python));alias.unlink()
 foreign=Path('/usr/bin/true').resolve(strict=True)
 if foreign.stat().st_uid!=os.getuid():
  expect('unsafe_path',lambda:scope._path(foreign))
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
  # Cross-check the unchanged legacy code identity, not only new-code output.
  assert {entry['codeSha256'] for entry in row['binding']['installations']}=={scope.code_identity()}
  # Observe real byte reads, without replacing any ownership/hash/admission check.
  reads=Counter();observing=[True]
  code_root=Path(scope.__file__).parents[2]
  bases=[code_root,*[installs[host][0] for host in ('claude','codex')]]
  def byte_reads(event,args):
   if observing[0] and event=='open' and isinstance(args[0],str) and args[0].endswith('.py'):
    for base_path in bases:
     if args[0].startswith(str(base_path/'core')+os.sep):reads[args[0]]+=1
  sys.addaudithook(byte_reads)
  try:
   assert scope._installed_binding([{'host':host,'root':str(installs[host][0])} for host in ('claude','codex')])==row['binding']['installations']
  finally:observing[0]=False
  for path,count in reads.items():
   limit=2 if path.startswith(str(code_root)+os.sep) or path.endswith('/context_learning/laya_adapter.py') else 1
   assert count<=limit,('redundant authorization byte reads',path,count,limit)
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
  hook=installs['claude'][0]/'hooks/run-hook';original_hook=hook.read_bytes()
  hook.write_bytes(original_hook+b'\n# altered hook\n')
  expect('changed',lambda:scope.require(root,policy,'local_train'));hook.write_bytes(original_hook)
  cached_source=installs['codex'][0]/'core/__pycache__/extra.py'
  cached_source.parent.mkdir(exist_ok=True);cached_source.write_text('extra executable source')
  expect('changed',lambda:scope.require(root,policy,'local_train'));cached_source.unlink()
  with patch('context_learning.project_scope.time.time',return_value=expiry/1000+1):expect('expired',lambda:scope.require(root,policy,'local_train'))
  expect('operation',lambda:scope.require(root,policy,'apply'))
  # Official enablement changes revoke a grant even if old code remains.
  save(home/'.claude/settings.json',{'enabledPlugins':{'qmd-auto-context@qmd-auto-context-marketplace':False}})
  expect('changed',lambda:scope.require(root,policy,'local_train'))
  (home/'.claude/settings.json').unlink()
  # Project host settings are part of the accepted binding even if absent at
  # proposal time; a later local enablement change cannot reuse the grant.
  local_config=project/'.codex/config.toml';local_config.parent.mkdir(exist_ok=True)
  local_config.write_text('[plugins]\n');local_config.chmod(0o600)
  expect('changed',lambda:scope.require(root,policy,'local_train'))
  local_config.unlink()
  with patch.object(scope,'_installations',return_value=[]):
   expect('changed',lambda:scope.require(root,policy,'local_train',fresh_inventory=True))
   expect('changed',lambda:scope.require(root,policy,'local_train'))
  # Fresh inventory is asked for this project, and an unchanged answer passes.
  current=[{'host':host,'root':str(installs[host][0])} for host in ('claude','codex')];seen=[]
  with patch.object(scope,'_installations',side_effect=lambda *args:seen.append(args) or current):
   assert scope.require(root,policy,'local_train',fresh_inventory=True)==row
   assert scope.require(root,policy,'local_train')==row
  assert seen==[(project,),(project,)],seen
  # A different effective root is a changed installation, never a renewal.
  with patch.object(scope,'_installations',return_value=[{'host':'claude','root':str(installs['codex'][0])},current[1]]):
   expect('installation_changed',lambda:scope.require(root,policy,'local_train',fresh_inventory=True))
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
# Project-effective host roots: the official CLIs are asked from the project,
# disabled historical installs never block, every enabled entry still counts.
from types import SimpleNamespace
from context_learning import history_activation as ha
with tempfile.TemporaryDirectory(prefix='qmd-effective-roots-',dir='/tmp') as folder:
 base=Path(folder).resolve();project=base/'project';project.mkdir(mode=0o700);home=base/'home'
 plugins=home/'.claude/plugins';VER='0.2-test';OFFICIAL='qmd-auto-context@qmd-auto-context-marketplace';STALE='qmd-auto-context@stale-qa'
 claude_cache=plugins/'cache/qmd-auto-context-marketplace/qmd-auto-context'/VER;claude_cache.mkdir(parents=True)
 stale_cache=plugins/'cache/stale-qa/qmd-auto-context/0.0-qa';stale_cache.mkdir(parents=True)
 codex_root=home/'.codex/plugins/cache/qmd-auto-context-marketplace/qmd-auto-context'/VER;codex_root.mkdir(parents=True)
 # An old QA directory marketplace reached through a symlinked temp parent.
 real=base/'real-tmp';linked=base/'linked-tmp';linked.symlink_to(real)
 save(real/'stale-market/.claude-plugin/marketplace.json',{'plugins':[{'name':'qmd-auto-context','version':'0.0-qa','source':'./plugins/qmd-auto-context'}]})
 save(plugins/'known_marketplaces.json',{'qmd-auto-context-marketplace':{'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}},
                                         'stale-qa':{'source':{'source':'directory','path':str(linked/'stale-market')}}})
 official={'scope':'project','projectPath':str(project),'version':VER,'installPath':str(claude_cache)}
 stale={'scope':'user','version':'0.0-qa','installPath':str(stale_cache)}
 def records(official_rows=None):
  save(plugins/'installed_plugins.json',{'version':2,'plugins':{OFFICIAL:official_rows or [official],STALE:[stale]}})
 records()
 def claude_list(official_enabled=True,stale_enabled=False,**extra):
  return [{'id':OFFICIAL,'enabled':official_enabled,**official,**extra},
          {'id':STALE,'enabled':stale_enabled,**stale},{'id':'unrelated@market','enabled':True}]
 def codex_list(*enabled):
  rows=[('qmd-auto-context-marketplace',VER),('old-qa','0.0-qa')]
  return {'installed':[{'name':'qmd-auto-context','marketplaceName':m,'version':v,'installed':True,'enabled':i in enabled}
                       for i,(m,v) in enumerate(rows)]}
 listing={};calls=[]
 trusted=base/'trusted-bin';trusted.mkdir(mode=0o700)
 for name in ('claude','codex'):
  executable=trusted/name;executable.write_text('stub');executable.chmod(0o700)
 def fake_run(argv,**kw):
  name=Path(argv[0]).name
  calls.append((name,kw.get('cwd'),kw['env'].get('HOME')))
  assert kw['env']['PATH']==str(trusted)
  out=listing[name]
  if isinstance(out,int):return SimpleNamespace(returncode=out,stdout='',stderr='')
  return SimpleNamespace(returncode=0,stdout=json.dumps(out),stderr='')
 resolve=lambda:ha._project_host_roots(home,project)
 with patch('context_learning.history_activation.subprocess.run',fake_run),patch.dict(os.environ,{'PATH':str(trusted)}):
  for name in ('CLAUDE_CONFIG_DIR','CODEX_HOME'):os.environ.pop(name,None)
  # Single effective root per host; the stale marketplace is never read.
  listing.update(claude=claude_list(),codex=codex_list(0))
  assert resolve()=={'claude':[claude_cache],'codex':[codex_root]}
  assert calls==[('claude',str(project),str(home)),('codex',str(project),str(home))],calls
  # A project-planted CLI earlier in PATH is never selected or inherited by
  # the host CLI's env-based interpreter.
  planted=project/'bin';planted.mkdir(mode=0o700)
  for name in ('claude','codex','node'):
   executable=planted/name;executable.write_text('planted');executable.chmod(0o700)
  with patch.dict(os.environ,{'PATH':str(planted)+os.pathsep+str(trusted)}):
   assert resolve()=={'claude':[claude_cache],'codex':[codex_root]}
  # The account-global inventory keeps rejecting that stale marketplace.
  expect('history_claude_marketplace_invalid',lambda:ha._claude_load_roots(home))
  # Multiple enabled roots are ambiguous; a matching one is never picked.
  listing['claude']=claude_list(stale_enabled=True)
  expect('claude_ambiguous',resolve)
  listing['claude']=claude_list(official_enabled=False)
  expect('claude_ambiguous',resolve)
  # An enabled invalid root (the stale directory itself) fails closed.
  listing['claude']=claude_list(official_enabled=False,stale_enabled=True)
  expect('history_claude_marketplace_invalid',resolve)
  listing['claude']=claude_list(official_enabled='yes')
  expect('claude_invalid',resolve)
  listing['claude']=claude_list();listing['codex']=codex_list(0,1)
  expect('codex_ambiguous',resolve)
  listing['codex']=codex_list()
  expect('codex_ambiguous',resolve)
  listing['codex']={'installed':[{'name':'qmd-auto-context','marketplaceName':'qmd-auto-context-marketplace','version':'../x','installed':True,'enabled':True}]}
  expect('codex_invalid',resolve)
  listing['codex']=codex_list(0)
  # Missing, duplicated or foreign-project installation records fail closed.
  listing['claude']=claude_list(version='0.3-other')
  expect('record_missing',resolve)
  records([official,dict(official)]);listing['claude']=claude_list()
  expect('record_missing',resolve)
  elsewhere={**official,'projectPath':str(base/'other-project')};records([elsewhere])
  listing['claude']=[{'id':OFFICIAL,'enabled':True,'version':VER,'installPath':str(claude_cache)}]
  expect('record_changed',resolve)
  records();listing['claude']=claude_list()
  assert resolve()['claude']==[claude_cache]
  # The Claude listing is account-wide: an enabled project install recorded for
  # another project is not loaded here and never makes this project ambiguous.
  other=base/'other-project-a';other.mkdir(mode=0o700)
  foreign={**official,'projectPath':str(other)}
  records([official,foreign])
  listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**foreign}]
  assert resolve()['claude']==[claude_cache]
  local_other={**foreign,'scope':'local'};records([official,foreign,local_other])
  listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**foreign},{'id':OFFICIAL,'enabled':True,**local_other}]
  assert resolve()['claude']==[claude_cache]
  # Only foreign installs: nothing is effective here.
  listing['claude']=[{'id':OFFICIAL,'enabled':True,**foreign}]
  expect('claude_ambiguous',resolve)
  # A foreign claim with no account record for that project fails closed.
  records([official]);listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**foreign}]
  expect('record_changed',resolve)
  # Two rows for this project, or this project plus an enabled user install, stay ambiguous.
  records([official,foreign]);listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**official}]
  expect('claude_ambiguous',resolve)
  listing['claude']=claude_list(stale_enabled=True)+[{'id':OFFICIAL,'enabled':True,**foreign}]
  expect('claude_ambiguous',resolve)
  # A project-bound row without a path, or an unknown scope, is invalid.
  listing['claude']=[{'id':OFFICIAL,'enabled':True,'scope':'project','version':VER,'installPath':str(claude_cache)}]
  expect('claude_invalid',resolve)
  listing['claude']=[{'id':OFFICIAL,'enabled':True,**official,'scope':'workspace'}]
  expect('claude_invalid',resolve)
  # Identity, not spelling: this project's own install listed under another
  # spelling of the same directory (APFS folds case) is never treated as foreign.
  variant=str(project).replace('/project','/PROJECT')
  if os.path.exists(variant) and variant!=str(project):
   alias={**official,'projectPath':variant};records([alias])
   listing['claude']=claude_list(stale_enabled=True)[1:2]+[{'id':OFFICIAL,'enabled':True,**alias}]
   expect('claude_ambiguous',resolve)
  # An enabled install recorded for a missing project cannot be proven foreign.
  gone={**official,'projectPath':str(base/'deleted-project')};records([official,gone])
  listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**gone}]
  expect('record_changed',resolve)
  # A stale record for a deleted project does not mask a valid foreign record.
  records([official,foreign,{**gone,'scope':'project'}])
  listing['claude']=claude_list()+[{'id':OFFICIAL,'enabled':True,**foreign}]
  assert resolve()['claude']==[claude_cache]
  records();listing['claude']=claude_list()
  assert resolve()['claude']==[claude_cache]
  # Freshness: a later CLI answer with a changed root is a different inventory.
  listing['codex']={'installed':[{'name':'qmd-auto-context','marketplaceName':'qmd-auto-context-marketplace','version':'0.3-new','installed':True,'enabled':True}]}
  expect('codex_record_missing',resolve)
  (codex_root.parent/'0.3-new').mkdir()
  assert resolve()['codex']==[codex_root.parent/'0.3-new']
  listing['codex']=codex_list(0)
  # CLI failure or a redirected host config home is not an inventory.
  listing['claude']=1
  expect('inventory_unavailable',resolve)
  listing['claude']=claude_list()
  with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(base/'elsewhere')}):
   expect('redirected',resolve)
 # project_scope maps any discovery failure or non-single answer to invalid.
 with patch.object(ha,'_project_host_roots',side_effect=ValueError('history_project_claude_ambiguous')):
  expect('project_scope_installation_invalid',lambda:scope._installations(project))
 with patch.object(ha,'_project_host_roots',return_value={'claude':[claude_cache,stale_cache],'codex':[codex_root]}):
  expect('project_scope_installation_invalid',lambda:scope._installations(project))
 # A project-selected QA marketplace is attested by host inventory plus the
 # exact installed source/hook bytes; the old official-id-only check remains
 # available solely on the account-global path.
 qa_roots={}
 for host in ('claude','codex'):
  target=home/('.'+host)/'plugins/cache/qmd-laya-fixture-qa/qmd-auto-context'/VER
  target.parent.mkdir(parents=True,exist_ok=True)
  target.mkdir()
  shutil.copytree(Path(scope.__file__).parents[1],target/'core',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copytree(Path(scope.__file__).parents[2]/'hooks',target/'hooks')
  save(target/('.'+host+'-plugin/plugin.json'),{'name':'qmd-auto-context','version':VER})
  qa_roots[host]=[target]
 with patch.object(Path,'home',return_value=home),patch.object(ha,'_project_host_roots',return_value=qa_roots):
  selected=scope._installations(project)
  assert selected==[{'host':host,'root':str(qa_roots[host][0])} for host in ('claude','codex')]
  assert len(scope._installed_binding(selected))==2
  hook=qa_roots['claude'][0]/'hooks/run-hook';saved=hook.read_bytes()
  hook.write_bytes(saved+b'\n# tampered\n')
  expect('code_changed',lambda:scope._installed_binding(selected))
  hook.write_bytes(saved)
print('general scope checks passed')
