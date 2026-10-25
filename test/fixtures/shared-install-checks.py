"""Formal install front door, actual worker/queue/hook guards, fake native QMD.
All data/model/endpoint identity are disposable synthetic fixtures.
"""
import contextlib,hashlib,io,json,os,runpy,subprocess,sys,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'core')
import setup_entry,qmd_endpoint,qmd_shared_store as S,dirty_queue
C=runpy.run_path('test/fixtures/shared-qmd-setup-checks.py')['SharedSetup']

class Install(C):
 def setUp(self):
  area=tempfile.TemporaryDirectory(prefix='qmd-install-fixture-',dir=Path.cwd().parent)
  self.addCleanup(area.cleanup)
  # Protected system temp cannot authorize collection scope. Use a disposable
  # external workspace area without weakening that product guard.
  with patch('tempfile.tempdir',area.name):super().setUp()
  self.endpoint_inspection=patch.object(qmd_endpoint,'inspection_available');self.endpoint_inspection.start()
  self.endpoint_start=patch.object(qmd_endpoint,'ensure',return_value={'pid':12345,'url':'http://127.0.0.1:19484'});self.endpoint_start.start()
  self.daemon=patch('shared_qmd_setup.coordinator._daemon_identity',return_value=None);self.daemon.start()
  self.endpoint_read=patch.object(qmd_endpoint,'read',return_value={'pid':12345,'port':19484,'url':'http://127.0.0.1:19484'});self.endpoint_read.start()
  self.endpoint_health=patch.object(qmd_endpoint,'healthy',return_value=True);self.endpoint_health.start()
  self.settings={'indexing':True,'collections':['fixture-docs','fixture-wiki'],'collectionPaths':{'fixture-docs':'docs','fixture-wiki':'.auto-context/wiki'},'collectionRoles':{'fixture-docs':'raw','fixture-wiki':'wiki'}}
  (self.project/'docs').mkdir();(self.project/'docs/source.md').write_text('Synthetic preserved source.\n')
  wiki=self.project/'.auto-context/wiki';wiki.mkdir();(wiki/'old.md').write_text('---\nschemaVersion: 1\nstatus: verified\n---\nSynthetic v1 card.\n')
  self.legacy=self.project/'.auto-context.json';self.legacy.write_text(json.dumps(self.settings)+'\n')
  (self.project/'.auto-context/settings.json').unlink()
  history=self.project/'.auto-context/context-learning';history.mkdir();(history/'history.jsonl').write_text('synthetic history\n')
  self.original_bytes={p:p.read_bytes() for p in [self.legacy,self.original,self.model,wiki/'old.md',history/'history.jsonl']}
  # Complete fake CLI for normal worker maintenance. No patch of worker,
  # durable claim/ACK, store publication, project routing or hook guard.
  self.qmd.write_text('''import sys,os,json,sqlite3,hashlib
from pathlib import Path
a=sys.argv[1:]
if '--version' in a:print('qmd 2.5.3');sys.exit(0)
if '--help' in a:print('qmd vsearch qmd search qmd get qmd collection add qmd update qmd embed');sys.exit(0)
p=Path(os.environ['INDEX_PATH']);assert p.is_relative_to(Path(os.environ['HOME']))
db=sqlite3.connect(p);c=Path(os.environ['QMD_CONFIG_DIR'])/'index.yml'
db.executescript('CREATE TABLE IF NOT EXISTS documents(collection TEXT,path TEXT,hash TEXT,active INTEGER);CREATE TABLE IF NOT EXISTS content(hash TEXT,doc TEXT);CREATE TABLE IF NOT EXISTS content_vectors(hash TEXT,seq INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER);CREATE TABLE IF NOT EXISTS store_collections(name TEXT,path TEXT,pattern TEXT);CREATE TABLE IF NOT EXISTS vectors_vec(hash_seq TEXT PRIMARY KEY,embedding "float[768] distance_metric=cosine");')
raw=c.read_text();model=json.loads(raw)['models']['embed'] if raw.lstrip().startswith('{') else next(l.split(': ',1)[1] for l in raw.splitlines() if l.startswith('  embed: '))
def config():
 rows=db.execute('SELECT name,path,pattern FROM store_collections').fetchall()
 c.write_text(('collections:\\n' if rows else 'collections: {}\\n')+''.join('  '+n+':\\n    path: '+path+'\\n    pattern: '+json.dumps(mask)+'\\n' for n,path,mask in rows)+'models:\\n  embed: '+model+'\\n')
if a[:2]==['collection','add']:
 name=a[a.index('--name')+1]
 if db.execute('SELECT 1 FROM store_collections WHERE name=?',(name,)).fetchone():print('already exists');sys.exit(1)
 mask=a[a.index('--mask')+1] if '--mask' in a else '**/*.md'
 db.execute('INSERT INTO store_collections VALUES (?,?,?)',(name,a[2],mask));config()
if a[0]=='update' or a[:2]==['collection','add']:
 for name,path in db.execute('SELECT name,path FROM store_collections').fetchall():
  for f in Path(path).rglob('*.md'):
   text=f.read_bytes();h=hashlib.sha256(text).hexdigest();rel=f.relative_to(path).as_posix()
   db.execute('DELETE FROM documents WHERE collection=? AND path=?',(name,rel));db.execute('INSERT INTO documents VALUES (?,?,?,1)',(name,rel,h))
   if not db.execute('SELECT 1 FROM content WHERE hash=?',(h,)).fetchone():db.execute('INSERT INTO content VALUES (?,?)',(h,text.decode()))
if a[0]=='embed':
 fingerprint=hashlib.sha256(Path(model).read_bytes()).hexdigest()
 for (h,) in db.execute('SELECT DISTINCT hash FROM documents').fetchall():
  if not db.execute('SELECT 1 FROM content_vectors WHERE hash=?',(h,)).fetchone():
   db.execute('INSERT INTO content_vectors VALUES (?,0,?,?,1)',(h,model,fingerprint));db.execute('INSERT OR REPLACE INTO vectors_vec VALUES (?,?)',(h+'_0',bytes(768*4)))
if a[0]=='query':
 if (Path(os.environ['HOME'])/'fail-query').exists():sys.exit(2)
 name=a[a.index('-c')+1];row=db.execute('SELECT path FROM documents WHERE collection=?',(name,)).fetchone()
 print(json.dumps([{'file':'qmd://'+name+'/'+row[0],'score':1}] if row else []))
if a[:2]==['collection','remove']:
 if not db.execute('SELECT 1 FROM store_collections WHERE name=?',(a[2],)).fetchone():sys.exit(1)
 db.execute('DELETE FROM store_collections WHERE name=?',(a[2],));db.execute('DELETE FROM documents WHERE collection=?',(a[2],));config()
if a[0]=='cleanup':db.execute('DELETE FROM content');db.execute('DELETE FROM content_vectors');db.execute('DELETE FROM vectors_vec')
db.commit();db.close()
''')
  self.qmd.chmod(0o700)
  os.environ['QMD_INDEX_WORKER_LOG']=str(self.home/'worker.log')
  self.flags=self.options+['--approve-store-activation','--approve-project-enrollment','--allow-live-host-sessions','--deadline-seconds','30']
 def tearDown(self):
  self.endpoint_health.stop();self.endpoint_read.stop();self.daemon.stop();self.endpoint_start.stop();self.endpoint_inspection.stop();super().tearDown()
 def entry(self,action='install',flags=None):
  out=io.StringIO()
  with contextlib.redirect_stdout(out):
   try:code=setup_entry.main([action,'--project',str(self.project),*(self.flags if flags is None else flags)])
   except SystemExit:return 1,{'status':'entry_missing'}
  return code,json.loads(out.getvalue())
 def preserved(self):
  for p,body in self.original_bytes.items():self.assertEqual(p.read_bytes(),body,str(p))
 def test_v1_install_finishes_common_route_search_and_actual_host_hooks(self):
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(row['status'],'qmd_ready');self.assertEqual(row['readiness']['guard'],'ready')
  self.assertTrue(row['qmdReady']);self.assertFalse(row['layaReady']);self.assertFalse(row['overallReady'])
  self.assertEqual(row['completionScope'],'qmd');self.assertEqual(json.loads((self.project/'.auto-context/shared-install-journal.json').read_text())['phase'],'completed')
  self.preserved();selected=S.select();self.assertIn(str(self.project),selected['approvedProjects'])
  check=setup_entry.check_update(self.project);self.assertTrue(check['qmdReady'],check);self.assertFalse(check['overallReady']);self.assertIn('laya_managed_runtime_not_prepared',check['reasons'])
  for host in ('claude','codex'):
   probe=self.home/'hook-probe.py';probe.write_text('import sys,json;sys.path.insert(0,'+repr(str(Path.cwd()/'core'))+');import qmd_route;print(json.dumps(qmd_route.project_paths(json.load(sys.stdin)["cwd"])))')
   p=subprocess.run(['bash','hooks/run-hook','recall',host],input=json.dumps({'cwd':str(self.project),'prompt':'synthetic docs'}),text=True,capture_output=True,env=dict(os.environ,QMD_CORE_RECALL_SCRIPT=str(probe),QMD_QUERY_FIXTURE='synthetic'))
   self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['INDEX_PATH'],selected['INDEX_PATH'])
 def test_plan_is_readonly_and_approval_is_required_before_copy(self):
  code,row=self.entry('plan-install');self.assertEqual(code,0,row);self.assertFalse((self.project/'.auto-context/settings.json').exists())
  code,row=self.entry(flags=self.options);self.assertNotEqual(code,0);self.assertFalse((self.project/'.auto-context/settings.json').exists());self.preserved()
 def test_selected_legacy_store_without_model_proof_blocks_install(self):
  settings=self.project/'.auto-context/settings.json'
  settings.write_text(json.dumps(self.settings)+'\n')
  self.legacy_selected_store()
  before=settings.read_bytes();selected=S.select();index=Path(selected['INDEX_PATH'])
  import sqlite3
  with sqlite3.connect(index) as db:
   physical_before=db.execute('SELECT name,path,pattern FROM store_collections').fetchall()
  for action in ('plan-install','install'):
   code,row=self.entry(action)
   self.assertNotEqual(code,0,row)
   self.assertEqual(row['status'],'blocked',row)
   self.assertIn('legacy_embedding_model_proof_required',row['blockers'])
   self.assertEqual(settings.read_bytes(),before)
  with sqlite3.connect(index) as db:
   self.assertEqual(db.execute('SELECT name,path,pattern FROM store_collections').fetchall(),physical_before)
  self.assertFalse((self.project/'.auto-context/shared-install-journal.json').exists())
 def test_interrupted_enqueue_resumes_same_tuple_without_reindexing_completed_work(self):
  self.assertEqual(self.entry()[0],0);first=S.select()['generation'];q=dirty_queue.queue_path();before=q.read_bytes()
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],first);self.assertEqual(q.read_bytes(),before);self.assertEqual(row['workerAttempts'],0);self.preserved()
 def test_query_failure_is_not_completed_and_same_install_resumes(self):
  # Fail final readiness only, after the one-file bootstrap proof.
  import shared_install
  original=shared_install._readiness
  def failure(*a,**k):raise ValueError('synthetic_readiness_interrupted')
  with patch.object(shared_install,'_readiness',side_effect=failure):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertNotEqual(row['status'],'completed');selected=S.select()['generation'];self.preserved()
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(S.select()['generation'],selected)
 def test_modern_v1_wiki_reuses_existing_store_without_bulk_migration(self):
  self.assertEqual(self.entry()[0],0)
  self.project=self.home/'second';(self.project/'.auto-context/wiki').mkdir(parents=True);(self.project/'docs').mkdir()
  settings=dict(self.settings,collections=['second-docs','second-wiki'],collectionPaths={'second-docs':'docs','second-wiki':'.auto-context/wiki'},collectionRoles={'second-docs':'raw','second-wiki':'wiki'})
  (self.project/'.auto-context/settings.json').write_text(json.dumps(settings));wiki=self.project/'.auto-context/wiki/old.md';wiki.write_text('---\nschemaVersion: 1\n---\nSynthetic second old card.\n');before=wiki.read_bytes()
  with patch.object(dirty_queue,'enqueue_collections',side_effect=OSError('synthetic_enqueue_interrupted')):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertNotEqual(row['status'],'completed')
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(wiki.read_bytes(),before);self.preserved()
 def test_identity_denial_is_fatal_before_any_write(self):
  with patch.object(qmd_endpoint,'inspection_available',side_effect=ValueError('qmd_endpoint_identity_unavailable')):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertFalse((self.project/'.auto-context/settings.json').exists());self.preserved()
 def test_first_enqueue_is_private_even_under_normal_umask(self):
  old=os.umask(0o022)
  try:dirty_queue.enqueue_collections({'fixture-docs':str(self.project/'docs')},project_root=str(self.project))
  finally:os.umask(old)
  self.assertEqual(dirty_queue.queue_path().stat().st_mode&0o777,0o600)
 def test_scope_change_at_enrollment_cannot_expand_initial_approval(self):
  import shared_qmd_setup as B
  original=B.enroll
  def changed(args,**kwargs):
   cfg=self.project/'.auto-context/settings.json';value=json.loads(cfg.read_text());value['collectionPaths']['fixture-docs']='new-private'
   (self.project/'new-private').mkdir();cfg.write_text(json.dumps(value));return original(args,**kwargs)
  with patch.object(B,'enroll',side_effect=changed):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertNotEqual(row['status'],'completed')
  self.assertFalse(dirty_queue.queue_path().exists(),'changed scope must never be enqueued')
 def test_existing_model_without_argument_is_revalidated_before_queue_write(self):
  self.assertEqual(self.entry()[0],0);q=dirty_queue.queue_path();before=q.read_bytes();self.model.write_bytes(b'GGUF changed model')
  flags=list(self.flags);i=flags.index('--embedding-model-file');del flags[i:i+2]
  code,row=self.entry(flags=flags);self.assertNotEqual(code,0);self.assertEqual(q.read_bytes(),before)
 def test_dead_endpoint_prevents_completed_receipt(self):
  self.assertEqual(self.entry()[0],0)
  with patch.object(qmd_endpoint,'healthy',return_value=False):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertNotEqual(row['status'],'completed')
 def test_readiness_timeout_is_deferred_75_and_resumable(self):
  import shared_install
  with patch.object(shared_install,'_readiness',side_effect=subprocess.TimeoutExpired(['synthetic-query'],1)):code,row=self.entry()
  self.assertEqual(code,75,row);self.assertEqual(row['status'],'deferred');self.preserved()
  self.assertEqual(self.entry()[0],0)
 def test_final_sidecar_contention_is_deferred_75(self):
  import shared_install
  ready=shared_install._readiness;snap=shared_install.maintenance.snapshot;done=[False]
  def verification(*a,**k):result=ready(*a,**k);done[0]=True;return result
  def snapshot(*a,**k):
   if done[0]:raise shared_install.maintenance.QueueBusy('synthetic_final_sidecar_busy')
   return snap(*a,**k)
  with patch.object(shared_install,'_readiness',side_effect=verification),patch.object(shared_install.maintenance,'snapshot',side_effect=snapshot):code,row=self.entry()
  self.assertEqual(code,75,row);self.assertEqual(row['status'],'deferred')
 def test_other_writer_settings_publication_is_not_removed(self):
  import shared_install
  link=shared_install.os.link;settings=self.project/'.auto-context/settings.json'
  def collision(source,destination,**kwargs):
   if Path(destination)==settings:
    settings.write_bytes(self.legacy.read_bytes());raise FileExistsError('synthetic competing settings')
   return link(source,destination,**kwargs)
  with patch.object(shared_install.os,'link',side_effect=collision):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertTrue(settings.exists());self.assertEqual(settings.read_bytes(),self.legacy.read_bytes());self.preserved()
 def test_prepare_failure_restores_only_owned_copy_then_same_install_resumes(self):
  with patch('shared_qmd_setup._build',side_effect=ValueError('synthetic_build_interruption')):code,row=self.entry()
  self.assertNotEqual(code,0);self.assertFalse((self.project/'.auto-context/settings.json').exists());self.preserved()
  self.assertEqual(self.entry()[0],0)
 def test_auto_update_missing_laya_foreign_path_partial_runtime_and_stale_pointer(self):
  import qmd_runtime,setup_guard
  spy=self.home/'unexpected-hook';core=self.home/'blocked-core.py';core.write_text('from pathlib import Path\nPath('+repr(str(spy))+').write_text("called")\n')
  external=self.home/'bin/qmd';external.write_text('#!/bin/sh\necho forbidden > '+str(spy)+'\n');external.chmod(0o700)
  for kind in ('v1-only','missing-models','runtime-prepared-only','stale-store-pointer'):
   if kind!='v1-only':
    (self.project/'.auto-context/settings.json').write_bytes(self.legacy.read_bytes())
   if kind=='runtime-prepared-only':qmd_runtime.prepare_existing(qmd_runtime.managed_root(),self.qmd,self.node,force_cpu=True)
   if kind=='stale-store-pointer':
    base=S._unlinked_root();base.mkdir(mode=0o700,parents=True,exist_ok=True);(base/'active.json').write_text('{}\n');(base/'active.json').chmod(0o600)
   if kind=='missing-models':self.model.rename(self.home/'preserved-model-marker')
   self.assertEqual(setup_guard.status(self.project)['status'],'setup_required')
   queue=self.home/('notice-'+kind);queue.mkdir(mode=0o700)
   env=dict(os.environ,QMD_UPDATE_HOOK_QUEUE_DIR=str(queue),QMD_CORE_RECALL_SCRIPT=str(core),QMD_CORE_POSTTOOL_SCRIPT=str(core),QMD_CORE_INDEX_SCRIPT=str(core),QMD_CORE_COMPILE_ENQUEUE_SCRIPT=str(core),QMD_CORE_UPDATE_SCRIPT=str(core))
   env.pop('QMD_SUPPRESS_NOTICE',None)
   notices=[]
   for repeat in range(2):
    for host in ('claude','codex'):
     for action in ('recall','index','compile','update-queued'):
      p=subprocess.run(['bash','hooks/run-hook',action,host],input=json.dumps({'cwd':str(self.project),'prompt':'synthetic docs','hook_event_name':'SessionStart'}),env=env,text=True,capture_output=True,timeout=8)
      self.assertEqual(p.returncode,0,p.stderr)
      if 'setup skill' in p.stdout:notices.append(p.stdout)
   self.assertEqual(len(notices),1,(kind,notices));self.assertFalse(spy.exists());self.assertFalse(list(queue.glob('*.job.json')))
   if kind=='missing-models':(self.home/'preserved-model-marker').rename(self.model)
   self.preserved()
 def test_install_releases_notice_and_preserves_single_plugin_host_registrations(self):
  import update_hook_queue
  files={self.home/'.claude/settings.json':b'{"enabledPlugins":{"qmd-auto-context@fixture":true}}\n',self.home/'.codex/config.toml':b'[plugins."qmd-auto-context@fixture"]\nenabled=true\n'}
  for file,body in files.items():file.parent.mkdir();file.write_bytes(body)
  queue=self.home/'notices';queue.mkdir(mode=0o700)
  out=io.StringIO()
  with contextlib.redirect_stdout(out):update_hook_queue._setup_notice(str(self.project),queue)
  self.assertIn('setup skill',out.getvalue());self.assertEqual(self.entry()[0],0)
  out=io.StringIO()
  with contextlib.redirect_stdout(out):update_hook_queue._setup_notice(str(self.project),queue)
  self.assertIn('laya_managed_runtime_not_prepared',out.getvalue())
  out=io.StringIO()
  with contextlib.redirect_stdout(out):update_hook_queue._setup_notice(str(self.project),queue)
  self.assertEqual(out.getvalue(),'')
  for file,body in files.items():self.assertEqual(file.read_bytes(),body)
 def test_completed_install_allows_query_preferences_and_reinstall(self):
  import setup_guard
  self.assertEqual(self.entry()[0],0)
  settings=self.project/'.auto-context/settings.json';value=json.loads(settings.read_text());value.update(topN=5,minScore=.2,queryTimeout=7);settings.write_text(json.dumps(value))
  self.assertEqual(setup_guard.status(self.project)['status'],'ready')
  code,row=self.entry();self.assertEqual(code,0,row);self.assertEqual(row['workerAttempts'],0)
 def test_readiness_expired_budget_is_deferred_not_complete(self):
  import shared_install
  ready=shared_install._readiness
  with patch.object(shared_install,'_readiness',side_effect=lambda root,selected,request,deadline:ready(root,selected,request,0)):
   code,row=self.entry()
  self.assertEqual(code,75,row);self.assertEqual(row['status'],'deferred')
 def test_old_completed_receipt_without_common_store_is_not_accepted(self):
  import qmd_runtime,install_update as C
  runtime=qmd_runtime.prepare_existing(qmd_runtime.managed_root(),self.qmd,self.node,force_cpu=True)
  qmd_runtime.activate(qmd_runtime.managed_root(),runtime['generation'])
  request={'runtimeRoots':{'qmd':str(qmd_runtime.managed_root())},'qmd':{'mode':'active'}}
  row={'schema':C.SCHEMA,'project':str(self.project),'phase':'activated','request':request,
   'activation':{'applied':{}},'staged':{'qmd':C._stage_qmd(request),'laya':{'mode':'defer'}}}
  C._atomic(self.project/'.auto-context/install-update-journal.json',row)
  code,result=self.entry('activate',flags=[]);self.assertNotEqual(code,0,result);self.assertNotIn(result['status'],('activated','activated_unchanged'));self.preserved()
 def assert_current_source_indexed(self):
  import sqlite3
  current=hashlib.sha256((self.project/'docs/source.md').read_bytes()).hexdigest()
  with sqlite3.connect(S.select()['INDEX_PATH']) as db:
   self.assertEqual(db.execute('SELECT hash FROM documents WHERE collection=? AND path=? AND active=1',('fixture-docs','source.md')).fetchone(),(current,))
 def test_source_edit_after_ack_requeues_approved_collections_and_proves_current_hash(self):
  import shared_install
  drain=shared_install._drain;enqueue=dirty_queue.enqueue_collections;changed=[False];queues=[]
  def after_ack(*args):
   result=drain(*args)
   if not changed[0]:
    (self.project/'docs/source.md').write_text('Synthetic changed after ACK.\n');changed[0]=True
   return result
  def queue(collections,**kwargs):queues.append(set(collections));return enqueue(collections,**kwargs)
  with patch.object(shared_install,'_drain',side_effect=after_ack),patch.object(dirty_queue,'enqueue_collections',side_effect=queue):code,row=self.entry()
  self.assertEqual(code,0,row);self.assert_current_source_indexed()
  # A fresh file's ctime may also change during this real filesystem run.
  # Every retry stays inside the approved roots and the edited source must
  # publish another queue intent after the initial full maintenance intent.
  self.assertTrue(any('fixture-docs' in names for names in queues[1:]),queues)
  self.assertTrue(all(names<={'fixture-docs','fixture-wiki'} for names in queues));self.preserved()
 def test_completed_resume_reindexes_source_changed_without_hook_event(self):
  self.assertEqual(self.entry()[0],0)
  (self.project/'docs/source.md').write_text('Synthetic changed while no v2 hook ran.\n')
  code,row=self.entry();self.assertEqual(code,0,row);self.assertGreater(row['workerAttempts'],0)
  self.assert_current_source_indexed();self.preserved()
 def test_source_changed_then_restored_during_drain_is_reindexed(self):
  import shared_install
  drain=shared_install._drain;source=self.project/'docs/source.md';initial=source.read_bytes();changed=[False]
  def temporary_edit(*args):
   if changed[0]:return drain(*args)
   changed[0]=True;source.write_text('Synthetic temporary worker input.\n')
   result=drain(*args);source.write_bytes(initial);return result
  with patch.object(shared_install,'_drain',side_effect=temporary_edit):code,row=self.entry()
  self.assertEqual(code,0,row);self.assert_current_source_indexed();self.preserved()
 def test_continuously_changing_source_defers_and_retains_queue_for_resume(self):
  import shared_install
  drain=shared_install._drain;edits=[0]
  def after_ack(*args):
   result=drain(*args);edits[0]+=1
   (self.project/'docs/source.md').write_text('Synthetic changing source '+str(edits[0])+'\n')
   return result
  with patch.object(shared_install,'_drain',side_effect=after_ack):code,row=self.entry()
  self.assertEqual(code,75,row);self.assertEqual(row['reason'],'sources_changed_during_resume')
  self.assertLessEqual(edits[0],3);self.assertTrue(dirty_queue.queue_path().read_bytes());self.preserved()
  code,row=self.entry();self.assertEqual(code,0,row);self.assert_current_source_indexed()

if __name__=='__main__':
 source_cases={
  'test_source_edit_after_ack_requeues_approved_collections_and_proves_current_hash',
  'test_completed_resume_reindexes_source_changed_without_hook_event',
  'test_source_changed_then_restored_during_drain_is_reindexed',
  'test_continuously_changing_source_defers_and_retains_queue_for_resume'}
 names=[n for n in Install.__dict__ if n.startswith('test_')]
 def grouped(group):
  if group not in ('all','baseline','sources'):raise SystemExit('unknown fixture group')
  return [n for n in names if group=='all' or (n in source_cases)==(group=='sources')]
 # Stable per-case CLI: `--list [group]` prints one case name per line and runs
 # nothing; `--case NAME` runs exactly one case in this process. The positional
 # group form is unchanged.
 if sys.argv[1:2]==['--list']:
  if len(sys.argv)>3:raise SystemExit('usage: --list [all|baseline|sources]')
  print('\n'.join(grouped(sys.argv[2] if len(sys.argv)>2 else 'all')));raise SystemExit(0)
 if sys.argv[1:2]==['--case']:
  if len(sys.argv)!=3 or sys.argv[2] not in names:raise SystemExit('unknown fixture case')
  selected=[sys.argv[2]]
 else:selected=grouped(sys.argv[1] if len(sys.argv)>1 else 'all')
 suite=unittest.TestSuite(Install(n) for n in selected)
 # Bounded diagnostics: case name and monotonic milliseconds only. A wrapper
 # timeout then names the active case and the cumulative cost before it.
 import signal,time
 began=time.monotonic_ns();active=[None,0]
 def ms(ns):return ns//1_000_000
 def diag(line):sys.stderr.flush();os.write(2,(line+'\n').encode())
 class TimedResult(unittest.TextTestResult):
  def startTest(self,test):
   now=time.monotonic_ns();active[:]=[test._testMethodName,now]
   diag(f'shared-install-case start {active[0]} total={ms(now-began)}ms');super().startTest(test)
  def stopTest(self,test):
   super().stopTest(test);now=time.monotonic_ns();name,start=active;active[:]=[None,0]
   diag(f'shared-install-case finish {name} {ms(now-start)}ms total={ms(now-began)}ms')
 def interrupted(signum,frame):
  # Record only, then terminate exactly as the default SIGTERM did. Detached
  # product children are not owned here and are never signalled.
  now=time.monotonic_ns();name,start=active
  case=f'{name} {ms(now-start)}ms' if name else 'none'
  os.write(2,f'\nshared-install-case interrupted {case} total={ms(now-began)}ms\n'.encode())
  signal.signal(signum,signal.SIG_DFL);os.kill(os.getpid(),signum)
 signal.signal(signal.SIGTERM,interrupted)
 result=unittest.TextTestRunner(verbosity=2,resultclass=TimedResult).run(suite)
 diag(f'shared-install-suite finish {ms(time.monotonic_ns()-began)}ms');raise SystemExit(not result.wasSuccessful())
