"""Owner-private disposable setup transactions; no model/teacher execution."""
import importlib.util
assert importlib.util.find_spec('project_learning_setup') is not None, 'project learning setup implementation missing'
import json,os,shutil,sqlite3,tempfile,time,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import project_learning_setup as setup
from context_learning import project_scope,learning_profile
from context_learning.laya_adapter import MODEL_FILES
from context_learning.store import canonical
from context_learning.shared import _project_key
from context_learning import adapter_binding
# Execute production imports from the copied installed Codex package, so guards
# validate an actual approved installation rather than a patched source path.
if 'QMD_SETUP_FIXTURE_PACKAGE' not in os.environ:
 with tempfile.TemporaryDirectory(prefix='qmd-setup-installed-',dir='/tmp') as outer:
  outer=Path(outer).resolve();home=outer/'home'
  package=home/'.codex/plugins/cache/qmd-auto-context-marketplace/qmd-auto-context/test'
  package.mkdir(parents=True)
  shutil.copytree(Path(setup.__file__).parent,package/'core',ignore=shutil.ignore_patterns('__pycache__'))
  shutil.copytree(Path(setup.__file__).parents[1]/'hooks',package/'hooks')
  result=subprocess.run([sys.executable,__file__],env={**os.environ,'PYTHONPATH':str(package/'core'),'QMD_SETUP_FIXTURE_PACKAGE':str(package)},capture_output=True,text=True)
  print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
  raise SystemExit(result.returncode)



def save(p,v):
 p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);p.write_text(canonical(v)+'\n');p.chmod(0o600)

def expect(reason,fn):
 try:fn()
 except ValueError as e:assert reason in str(e),(reason,str(e))
 else:raise AssertionError('accepted '+reason)

with tempfile.TemporaryDirectory(prefix='qmd-learning-setup-',dir='/tmp') as temp:
 base=Path(temp).resolve();home=Path(os.environ['QMD_SETUP_FIXTURE_PACKAGE']).parents[5]
 project=base/'project';project.mkdir(mode=0o700)
 settings={'indexing':True,'collections':['docs'],'collectionPaths':{'docs':'.'},'contextLearning':{'capture':False,'autoCycle':False,'liveSelection':True}}
 save(project/'.auto-context/settings.json',settings)
 (project/'.auto-context').chmod(0o755)
 from context_learning.runtime_setup import default_managed_root
 root=default_managed_root(home).parent.parent/'live-selection'/_project_key(project);root.mkdir(mode=0o700,parents=True)
 model=base/'model';model.mkdir(mode=0o700)
 for name in MODEL_FILES:
  p=model/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
 save(model/'rl_agent_config.json',{'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256})
 python=base/'python';python.write_text('runtime');python.chmod(0o700)
 installs={}
 for host in ('claude','codex'):
  p=home/('.'+host)/'plugins/cache/qmd-auto-context-marketplace/qmd-auto-context/test';p.mkdir(parents=True,exist_ok=True)
  if host=='claude':shutil.copytree(Path(project_scope.__file__).parents[1],p/'core',ignore=shutil.ignore_patterns('__pycache__'))
  if host=='claude':shutil.copytree(Path(project_scope.__file__).parents[2]/'hooks',p/'hooks')
  save(p/('.'+host+'-plugin/plugin.json'),{'name':'qmd-auto-context','version':'test'})
  save(p/'package.json',{'name':'qmd-auto-context','version':'test'})
  installs[host]=[p]
 save(home/'.claude/plugins/known_marketplaces.json',{'qmd-auto-context-marketplace':{'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}})
 save(home/'.claude/plugins/installed_plugins.json',{'plugins':{'qmd-auto-context@qmd-auto-context-marketplace':[{'version':'test','installPath':str(installs['claude'][0])}]}})
 codex={'name':'qmd-auto-context','marketplaceName':'qmd-auto-context-marketplace','version':'test','installed':True,'enabled':True,'source':{'source':'github','repo':'zbdulee/qmd-auto-context'}}
 adapter=installs['codex'][0]/'core/context_learning/laya_adapter.py'
 save(root/'live-selector.json',{'schema':'qmd-live-selector-v1','trainerArgv':[str(python),str(adapter),str(model)],'timeoutSeconds':30,'maxRssMib':4096})
 artifact=base/'base.artifact';artifact.write_text('immutable base');artifact.chmod(0o600)
 save(root/'active-checkpoint.json',{'artifact_path':str(artifact),'artifact_sha256':setup.file_sha(artifact),'cycle_id':'base'})
 db=root/'shared-learning.sqlite3'
 with sqlite3.connect(db) as conn:conn.execute('CREATE TABLE captured (body TEXT)');conn.execute("INSERT INTO captured VALUES ('original capture')")
 db.chmod(0o600)
 for name in ('laya-inference-binding.json','laya-inference-manifest.json','laya-inference-proof.json'):save(root/name,{'original':name})
 original_settings=(project/'.auto-context/settings.json').read_bytes();original_pointer=(root/'active-checkpoint.json').read_bytes()
 expiry=int(time.time()*1000)+3600000;cutoff=int(time.time()*1000)
 with patch.object(Path,'home',return_value=home),patch('context_learning.history_activation._discover_host_roots',return_value=installs),patch('context_learning.adapter_binding._codex_installed',return_value=codex):
  # Preparation must reject runtime identities that scoped activation refuses,
  # before writing a journal, policy, settings or namespace data.
  live_path=root/'live-selector.json';original_live=live_path.read_bytes()
  def reject_runtime(runtime):
   live=json.loads(original_live);live['trainerArgv'][0]=str(runtime);save(live_path,live)
   before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   try:
    expect('setup_runtime_invalid',lambda:setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff))
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert (project/'.auto-context/settings.json').read_bytes()==original_settings
   finally:live_path.write_bytes(original_live)
  foreign=Path('/usr/bin/true').resolve(strict=True)
  if foreign.stat().st_uid!=os.getuid():reject_runtime(foreign)
  alias=base/'runtime-hardlink';os.link(python,alias)
  try:reject_runtime(python)
  finally:alias.unlink()
  # A venv-style symlink to a safe owned target remains supported and bound.
  alias=base/'runtime-symlink';alias.symlink_to(python)
  live=json.loads(original_live);live['trainerArgv'][0]=str(alias);save(live_path,live)
  try:
   before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   linked=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
   assert linked['runtime']['path']==str(alias) and linked['runtime']['resolved']==str(python)
   assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
   assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  finally:live_path.write_bytes(original_live);alias.unlink()
  before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
  plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  assert plan['settings']['contextLearning']['capture'] is True and plan['settings']['contextLearning']['autoCycle'] is True
  assert plan['policy']['goldModel']=='gpt-6-luna' and plan['policy']['baseArtifact']==str(artifact)
  expect('acceptance',lambda:setup.activate(plan,accepted_sha256='0'*64))
  # Exact review CAS protects concurrent session/user configuration edits.
  save(project/'.auto-context/settings.json',{**settings,'topN':4})
  expect('changed',lambda:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan)))
  (project/'.auto-context/settings.json').write_bytes(original_settings)
  # Interrupted installation must keep hooks gated and leave recovery actionable.
  def fault(at):
   if at=='scope_written':raise RuntimeError('power loss')
  try:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan),fault=fault)
  except RuntimeError:pass
  else:raise AssertionError('fault did not run')
  assert json.loads((root/setup.NAME).read_text())['phase']=='installing'
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  result=setup.recover(project,root)
  assert result['status']=='recovered'
  assert not (root/'shared-policy.json').exists()
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  for boundary in ('prepared','intent','settings_written','profile_written'):
   plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
   def crash(at):
    if at==boundary:raise RuntimeError('interrupted '+boundary)
   try:setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan),fault=crash)
   except RuntimeError:pass
   else:raise AssertionError('missing boundary '+boundary)
   if boundary=='prepared':save(root/'shared-policy.json',{'external':'concurrent creation'})
   setup.recover(project,root)
   assert (project/'.auto-context/settings.json').read_bytes()==original_settings
   if boundary=='prepared':
    assert json.loads((root/'shared-policy.json').read_text())=={'external':'concurrent creation'}
    (root/'shared-policy.json').unlink()
  plan=setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff)
  result=setup.activate(plan,accepted_sha256=setup.proposal_sha256(plan))
  assert result['status']=='activated'
  cfg=json.loads((project/'.auto-context/settings.json').read_text())
  assert cfg['contextLearning']['capture'] and cfg['contextLearning']['autoCycle']
  assert cfg['sharedLearning']['enabled'] and cfg['sharedLearning']['autoApply']
  assert json.loads((root/setup.NAME).read_text())['phase']=='activated'
  policy=json.loads((root/'shared-policy.json').read_text())
  assert project_scope.require(root,policy,'local_train')['projectRoot']==str(project)
  assert learning_profile.require(root,policy,'apply')['autoApply']
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  target_settings=(project/'.auto-context/settings.json').read_bytes()
  save(project/'.auto-context/settings.json',{**cfg,'topN':3})
  expect('settings_changed',lambda:setup.recover(project,root))
  (project/'.auto-context/settings.json').write_bytes(target_settings)
  policy_bytes=(root/'shared-policy.json').read_bytes()
  save(root/'shared-policy.json',{**policy,'maxGoldCalls':22})
  expect('configuration_changed',lambda:setup.recover(project,root))
  (root/'shared-policy.json').write_bytes(policy_bytes)
  with sqlite3.connect(Path(result['backupRoot'])/'namespace/shared-learning.sqlite3') as conn:
   assert conn.execute('SELECT body FROM captured').fetchall()==[('original capture',)]
  # Recovery restores configuration only, preserving any subsequently captured data.
  db=root/'shared-learning.sqlite3'
  with sqlite3.connect(db) as conn:conn.execute("INSERT INTO captured VALUES ('new capture')")
  db.chmod(0o600)
  setup.recover(project,root)
  assert (project/'.auto-context/settings.json').read_bytes()==original_settings
  with sqlite3.connect(db) as conn:assert conn.execute('SELECT body FROM captured').fetchall()==[('original capture',),('new capture',)]
  assert (root/'active-checkpoint.json').read_bytes()==original_pointer
  # Symlink inputs are rejected before dereferencing them.
  p=root/'live-selector.json';raw=p.read_bytes();p.unlink();target=base/'other';target.write_bytes(raw);target.chmod(0o600);p.symlink_to(target)
  expect('unsafe',lambda:setup.prepare(project,root,expires_at_unix_ms=expiry,not_before_unix_ms=cutoff))
print('project learning setup checks passed')
