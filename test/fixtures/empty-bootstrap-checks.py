import contextlib, io, json, os, pathlib, sqlite3, sys, tempfile
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[2] / 'core'))
import install_update, runtime_update, setup_entry, setup_guard, qmd_route

with tempfile.TemporaryDirectory(prefix='qmd-empty-bootstrap-check-') as name:
 root=pathlib.Path(name).resolve(); project=root/'project';project.mkdir()
 home=root/'home';home.mkdir();os.environ['HOME']=str(home)
 os.environ.pop('QMD_SETUP_GUARD_FIXTURE',None)
 # Historical per-project migration proof remains isolated from the new
 # production plugin-wide route. No user DB or active pointer is involved.
 original_route=qmd_route.project_paths
 qmd_route.project_paths=lambda project_root,**kwargs: original_route(project_root,fixture_env=True,**{**kwargs,'reject_conflicts':False})
 auto=project/'.auto-context';auto.mkdir();wiki=auto/'wiki';wiki.mkdir()
 marker=project/'.qmd-topical-v2-project';marker.write_text('explicit opt-in\n');marker.chmod(0o600)
 (auto/'settings.json').write_text(json.dumps({'indexing':True,'collections':['wiki'],
  'collectionPaths':{'wiki':'.auto-context/wiki'},'collectionRoles':{'wiki':'wiki'},'recallStrategy':'wikiOnly'}))
 config=project/'qmd-config/index.yml';config.parent.mkdir()
 config.write_text(f'collections:\n  wiki:\n    path: {wiki}\nmodels:\n  embed: synthetic-model\n')
 cache=project/'cache';cache.mkdir()
 fake_qmd=project/'qmd';fake_qmd.write_text('synthetic\n')
 install_update.supported_platform=lambda:True
 setup_entry._qmd_choice=lambda args,inv:{'mode':'active'}
 def call(*extra):
  args=['plan','--project',str(project),'--qmd-config',str(config),
   '--model-cache',str(cache),'--expected-dimension','768','--approve-index-execution',*extra]
  out=io.StringIO()
  with contextlib.redirect_stdout(out):code=setup_entry.main(args)
  return code,json.loads(out.getvalue())
 code,ordinary=call();assert code==1 and ordinary['blockers']==['published_v2_wiki_required'],ordinary
 code,ready=call('--bootstrap-empty-wiki');assert code==0 and ready['migrationScope']=='v2_empty_bootstrap',ready
 request=ready['request'];assert request['index']['sourceIndex'] is None and request['index']['bootstrapEmptyWiki'] is True
 assert install_update._wiki_corpus(project,request)['documents']==[]
 for relative in ('.auto-context.json','qmd-db/index.sqlite','.auto-context/wiki/index.md'):
  target=project/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('old\n')
  code,blocked=call('--bootstrap-empty-wiki');assert code==1 and 'empty_bootstrap_requires_new_project' in blocked['blockers'],blocked
  target.unlink()
 source=project/'old.sqlite';source.write_text('old')
 code,blocked=call('--bootstrap-empty-wiki','--source-index',str(source))
 assert code==1 and 'empty_bootstrap_requires_new_project' in blocked['blockers']
 source.unlink()
 def old_index(path,collection_path):
  path.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect(path) as db:
   db.executescript('CREATE TABLE store_collections(name TEXT PRIMARY KEY,path TEXT);'
    'CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);'
    'CREATE TABLE content(hash TEXT);'
    'CREATE TABLE content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT);'
    'CREATE TABLE vectors_vec(embedding "float[768] distance_metric=cosine");')
   db.execute('INSERT INTO store_collections VALUES (?,?)',('prior',str(collection_path)))
   db.execute('INSERT INTO documents VALUES (?,?,?,1)',('prior','old.md','old-hash'))
   db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',
              ('old-hash',0,'synthetic-model','prior-fingerprint'))
 def refused(request):
  try:install_update._wiki_corpus(project,request)
  except ValueError as exc:assert str(exc)=='empty_bootstrap_requires_new_project',exc
  else:raise AssertionError('existing_index_was_accepted')
 custom=project/'custom-active.sqlite'
 os.environ['INDEX_PATH']=str(custom)
 old_index(custom,project/'sources')
 code,blocked=call('--bootstrap-empty-wiki')
 assert code==1 and 'empty_bootstrap_requires_new_project' in blocked['blockers'],blocked
 custom.unlink()
 code,local_ready=call('--bootstrap-empty-wiki')
 assert code==0 and local_ready['request']['index']['bootstrapIndexRoute']==str(custom)
 old_index(custom,project/'sources')
 refused(local_ready['request']) # A project DB appeared after plan.
 os.environ.pop('INDEX_PATH')
 refused(local_ready['request']) # The recorded route survives an environment change.
 custom.unlink()
 os.environ['XDG_CACHE_HOME']=str(project/'local-cache')
 default=project/'local-cache/qmd/index.sqlite'
 code,default_ready=call('--bootstrap-empty-wiki')
 assert code==0 and default_ready['request']['index']['bootstrapIndexRoute']==str(default)
 old_index(default,project/'sources')
 refused(default_ready['request'])
 code,blocked=call('--bootstrap-empty-wiki')
 assert code==1 and 'empty_bootstrap_requires_new_project' in blocked['blockers'],blocked
 default.unlink();os.environ.pop('XDG_CACHE_HOME')
 shared=home/'.cache/qmd/index.sqlite'
 old_index(shared,root/'other-project')
 os.environ['INDEX_PATH']=str(shared)
 code,shared_ready=call('--bootstrap-empty-wiki')
 assert code==0 and shared_ready['request']['index']['sourceIndex'] is None,shared_ready
 with sqlite3.connect(shared) as db:
  db.execute('UPDATE store_collections SET path=?',(str(project),))
 refused(shared_ready['request']) # Global DB became project-related after plan.
 os.environ.pop('INDEX_PATH');shared.unlink()
 assert install_update._wiki_corpus(project,request)['documents']==[]
 def fake_runner(command,*,env,**kw):
  dbpath=pathlib.Path(env['INDEX_PATH']);op=command[1:]
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
   elif op[:2]==['collection','remove']:
    db.execute('DELETE FROM documents')
   elif op[0]=='cleanup':
    db.execute('DELETE FROM content_vectors')
  return SimpleNamespace(returncode=0,stdout='[]' if op[0]=='vsearch' else '',stderr='')
 staged=runtime_update.stage_shadow_index(project,qmd_bin=fake_qmd,config_file=config,
  wiki_dir=wiki,model_cache=cache,expected_model='synthetic-model',expected_dimension=768,
  allow_execution=True,bootstrap_empty=True,runner=fake_runner)
 proof=json.loads((pathlib.Path(staged['generation'])/'prepared.json').read_text())
 assert proof['bootstrapModelFingerprints']==[{'model':'synthetic-model','fingerprint':'synthetic-fingerprint'}],proof
 assert proof['inspection']['activeDocuments']==0 and proof['inspection']['dimension']==768
 assert (pathlib.Path(staged['generation'])/'config/index.yml').read_bytes()==config.read_bytes()
 install_update._check_shadow_documents(staged['index'],{'collection':'wiki','documents':[]},'synthetic-model')
 runtime_update.activate_shadow_index(project,staged['generation'])
 assert runtime_update.select_runtime(project)['INDEX_PATH']==staged['index']
 assert setup_guard.status(project)['status']=='ready'
 with sqlite3.connect(staged['index']) as db:
  db.execute('INSERT INTO documents VALUES (?,?,?,1)',('wiki','topical-v2/g/first.md','real-hash'))
  db.execute('INSERT INTO content_vectors VALUES (?,?,?,?)',('real-hash',0,'synthetic-model','synthetic-fingerprint'))
 assert runtime_update.select_runtime(project)['INDEX_PATH']==staged['index']
 with sqlite3.connect(staged['index']) as db:
  db.execute("UPDATE content_vectors SET model='wrong-model'")
 try:runtime_update.select_runtime(project)
 except ValueError as exc:assert str(exc)=='index_pointer_changed',exc
 else:raise AssertionError('wrong_model_was_accepted')
 print(json.dumps({'freshPlan':True,'oldConfigBlocked':True,'oldDbBlocked':True,
  'wikiPageBlocked':True,'sourceIndexBlocked':True,'customIndexBlocked':True,
  'postPlanIndexRaceBlocked':True,'defaultCacheIndexBlocked':True,
  'globalProjectCollectionBlocked':True,'emptyProbeRemoved':True,
  'guardReadyAfterActivation':True,'firstModelBound':True,'wrongModelRejected':True}))
