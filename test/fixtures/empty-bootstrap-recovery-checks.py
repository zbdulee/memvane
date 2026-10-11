"""Bootstrap coordinator crash/retry, late DB route rejection, and rollback."""
import contextlib,hashlib,io,json,os,pathlib,sqlite3,sys,tempfile
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2] / 'core'))
import install_update as coordinator, runtime_update, setup_entry, setup_guard, qmd_route
with tempfile.TemporaryDirectory(prefix='qmd-bootstrap-recovery-') as name:
 root=pathlib.Path(name).resolve();project=root/'project';project.mkdir();home=root/'home';home.mkdir()
 os.environ['HOME']=str(home);os.environ.pop('QMD_SETUP_GUARD_FIXTURE',None)
 original_route=qmd_route.project_paths
 qmd_route.project_paths=lambda project_root,**kwargs: original_route(project_root,fixture_env=True,**{**kwargs,'reject_conflicts':False})
 auto=project/'.auto-context';auto.mkdir();wiki=auto/'wiki';wiki.mkdir()
 marker=project/'.qmd-topical-v2-project';marker.write_text('explicit opt-in\n');marker.chmod(0o600)
 (auto/'settings.json').write_text(json.dumps({'indexing':True,'collections':['wiki'],
  'collectionPaths':{'wiki':'.auto-context/wiki'},'collectionRoles':{'wiki':'wiki'},'recallStrategy':'wikiOnly'}))
 config=project/'qmd-config/index.yml';config.parent.mkdir();config.write_text(f'collections:\n  wiki:\n    path: {wiki}\nmodels:\n  embed: synthetic-model\n')
 cache=project/'cache';cache.mkdir();fake_qmd=project/'qmd';fake_qmd.write_text('synthetic\n')
 interpreter=project/'synthetic-interpreter';interpreter.write_text('#!/bin/sh\nexit 0\n');interpreter.chmod(0o700)
 venv=project/'venv/bin';venv.mkdir(parents=True)
 (venv/'python').symlink_to(interpreter)
 from types import SimpleNamespace as Args
 choice=setup_entry._laya_choice(Args(laya_mode='reuse',laya_executable=str(venv/'python'),
  laya_adapter=str(pathlib.Path(setup_entry.__file__).parent/'context_learning/laya_adapter.py'),
  laya_model_dir=str(cache)),{}, {})
 assert choice['executable']==str(venv/'python') and choice['mode']=='reuse'
 custom=project/'custom-active.sqlite';os.environ['INDEX_PATH']=str(custom)
 coordinator.supported_platform=lambda:True
 setup_entry._qmd_choice=lambda args,inv:{'mode':'active'}
 coordinator._stage_qmd=lambda request:{'mode':'active','generation':'synthetic-active','wrapper':str(fake_qmd)}
 coordinator._check_qmd=lambda request,staged:staged
 coordinator._check_qmd_route=lambda staged:staged
 coordinator._daemon_identity=lambda:None
 original_stage=runtime_update.stage_shadow_index
 interrupt=[True]
 def fake_runner(command,*,env,**kwargs):
  dbpath=pathlib.Path(env['INDEX_PATH']);op=command[1:]
  if op[:2]==['collection','add'] and interrupt[0]:
   interrupt[0]=False
   raise SystemExit('synthetic_probe_process_interruption')
  with sqlite3.connect(dbpath) as db:
   if op[0]=='update':
    db.executescript('CREATE TABLE IF NOT EXISTS documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);'
     'CREATE TABLE IF NOT EXISTS content(hash TEXT);'
     'CREATE TABLE IF NOT EXISTS content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT);'
     'CREATE TABLE IF NOT EXISTS store_collections(name TEXT);')
   elif op[:2]==['collection','add']:
    db.execute('INSERT INTO documents VALUES (?,?,?,1)',(op[4],'probe.md','probe-hash'))
   elif op[0]=='embed':
    if db.execute('SELECT 1 FROM documents WHERE active=1').fetchone():
     db.execute('CREATE TABLE IF NOT EXISTS vectors_vec(embedding "float[768] distance_metric=cosine")')
     db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',('probe-hash',0,'synthetic-model','synthetic-fingerprint'))
   elif op[:2]==['collection','remove']:db.execute('DELETE FROM documents')
   elif op[0]=='cleanup':db.execute('DELETE FROM content_vectors')
  return SimpleNamespace(returncode=0,stdout='[]' if op[0]=='vsearch' else '',stderr='')
 def stage(*args,**kwargs):return original_stage(*args,**{**kwargs,'runner':fake_runner})
 runtime_update.stage_shadow_index=stage
 out=io.StringIO()
 with contextlib.redirect_stdout(out):code=setup_entry.main(['plan','--project',str(project),'--bootstrap-empty-wiki',
  '--qmd-config',str(config),'--model-cache',str(cache),'--expected-dimension','768','--approve-index-execution'])
 plan=json.loads(out.getvalue());assert code==0 and plan['status']=='ready_to_prepare',plan
 req=auto/'synthetic-request.json';req.write_text(json.dumps(plan['request']));req.chmod(0o600)
 try:coordinator.prepare(project,str(req))
 except SystemExit as exc:assert str(exc)=='synthetic_probe_process_interruption',exc
 else:raise AssertionError('synthetic_interruption_not_raised')
 assert not (auto/'qmd-index-active.json').exists()
 assert coordinator.status(project)['phase']=='preparing'
 interrupted=list((auto/'qmd-index-generations').glob('*/index.sqlite'))
 assert len(interrupted)==1,interrupted
 retry=coordinator.prepare(project,str(req))
 assert retry['status']=='prepared',retry
 prepared=coordinator.status(project)
 assert prepared['staged']['index']['generation']!=str(interrupted[0].parent)
 assert not (auto/'qmd-index-active.json').exists()
 with sqlite3.connect(custom) as db:
  db.executescript('CREATE TABLE store_collections(name TEXT,path TEXT);'
   'CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);')
  db.execute('INSERT INTO store_collections VALUES (?,?)',('prior',str(project/'sources')))
  db.execute('INSERT INTO documents VALUES (?,?,?,1)',('prior','old.md','old-hash'))
 before=hashlib.sha256(custom.read_bytes()).hexdigest()
 rejected=coordinator.activate(project)
 assert rejected['reason']=='empty_bootstrap_requires_new_project',rejected
 assert not (auto/'qmd-index-active.json').exists()
 assert hashlib.sha256(custom.read_bytes()).hexdigest()==before
 custom.unlink()
 activated=coordinator.activate(project)
 assert activated['status']=='activated',activated
 assert setup_guard.status(project)['status']=='ready'
 assert runtime_update.select_runtime(project)['INDEX_PATH']==prepared['staged']['index']['index']
 rolled=coordinator.rollback(project)
 assert rolled['status']=='rolled_back',rolled
 assert not (auto/'qmd-index-active.json').exists()
 assert runtime_update.select_runtime(project) is None
 assert setup_guard.status(project)['status']!='ready'
 print(json.dumps({'interruptedProbeNoPointer':True,'retryPreparedFreshGeneration':True,
  'lateCustomDbCutoverRejected':True,'lateCustomDbBytesPreserved':True,
  'bootstrapActivated':True,'rollbackRemovedPointer':True,
  'layaVenvSymlinkPlanAccepted':True}))
