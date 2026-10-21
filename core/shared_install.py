"""Explicit preserving file-index install; no host auto-update permission grant.

Existing runtime/model are reused. Only normal worker writes the selected DB.
Incomplete selected stores are preserved for forward resume, never rolled back.
"""
from pathlib import Path
import copy,json,os,subprocess,tempfile,time,getpass,math,sqlite3,hashlib,stat
from urllib.parse import quote
import config,install_update as C,shared_qmd_setup as B,qmd_shared_store as S
import qmd_runtime as R,qmd_route,setup_guard,qmd_endpoint as E
import dirty_queue,sqlite_read,install_maintenance as maintenance
import vector_integrity,qmd_write_fence,setup_readiness
import install_deadline,install_evidence as V

SCHEMA='qmd-shared-install-v1'
JOURNAL='shared-install-journal.json'
ROOT=Path(__file__).resolve().parent.parent

def _selected():
 pointer=S._unlinked_root()/'active.json'
 if pointer.exists() or pointer.is_symlink():return S.select()
 return None

def _save(auto,row):
 C._atomic(auto/JOURNAL,row)
 fd=os.open(auto,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def _journal(auto):
 p=auto/JOURNAL
 if not p.exists() and not p.is_symlink():return None
 row=json.loads(S._private_file(p).read_text())
 if row.get('schema')!=SCHEMA or not isinstance(row.get('request'),dict):raise ValueError('invalid_shared_install_journal')
 if row.get('phase') not in ('preparing','enrolling','indexing','deferred','failed_preparing','recovery_required','completed'):raise ValueError('invalid_shared_install_phase')
 if row.get('requestSha256')!=C.digest(row['request']):raise ValueError('shared_install_intent_changed')
 root=Path(row['request']['project']);source=row.get('source',{})
 paths={'modern':root/'.auto-context/settings.json','legacy_root':root/'.auto-context.json','legacy_agents':root/'.agents/qmd-recall.json'}
 if source.get('kind') not in paths or source.get('path')!=str(paths[source['kind']]):raise ValueError('invalid_install_settings_source')
 return row

def plan(args):
 root=C.project_root(args.project);source=C._config_source(root)
 if source['kind']=='missing':raise ValueError('project_settings_required')
 cfg=config.normalize_config(json.loads(Path(source['path']).read_text()))
 if cfg.get('indexing') is not True:raise ValueError('project_index_optin_required')
 owners=S._collection_owners(root,cfg)
 if not any(v['indexed'] for v in owners.values()):raise ValueError('indexed_collections_required')
 if args.laya_mode!='defer':raise ValueError('shared_install_laya_preparation_is_separate')
 if args.source_index or args.reviewed_v2_mappings or args.bootstrap_empty_wiki:raise ValueError('shared_install_preserves_originals_no_topical_migration')
 if args.store_scope and args.store_scope!=[str(root)]:raise ValueError('shared_install_single_approved_root_required')
 selected=_selected();model=None
 if args.embedding_model_file:model=B._model(args.embedding_model_file)
 if selected:
  journal=S.root()/B.JOURNAL
  if not journal.exists() and not journal.is_symlink() and selected.get('approvedProjects') is None:
   # A legacy shared DB can retain its registered owners, but it has no
   # binding from historical vectors to an exact model file. The normal
   # installer runs a whole-store embed, so a new collection would risk
   # changing unrelated vectors or mixing model generations. A separately
   # reviewed GGUF does not prove the bytes used for the old vectors.
   return {'status':'blocked','blockers':['legacy_embedding_model_proof_required'],
    'changesApplied':0,'storeSelected':True,'laya':'deferred_no_learning'}
  bootstrap=json.loads(S._private_file(journal).read_text())
  proof=bootstrap['proof']
  requested_backend=args.qmd_backend or ('cpu' if args.qmd_cpu else None)
  if requested_backend and requested_backend!=bootstrap['request']['backend']:raise ValueError('runtime_backend_changed_requires_review')
  current={'path':proof['modelPath'],'sha256':proof['modelSha256']}
  if B._model(current['path'])!=current:raise ValueError('embedding_model_changed_requires_review')
  if model and model!=current:raise ValueError('embedding_model_changed_requires_review')
  model=model or current;dimension=args.expected_dimension or proof['dimension']
  if dimension!=proof['dimension']:raise ValueError('embedding_dimension_changed_requires_review')
 else:
  if not model:raise ValueError('local_embedding_model_required')
  dimension=args.expected_dimension
  if type(dimension) is not int or dimension<1:raise ValueError('expected_embedding_dimension_required')
  # Read-only legacy inventory cannot publish modern settings for _scopes.
  # Validate all other existing initializer inputs and prospective scope SHA.
  prospective=copy.copy(args);prospective.store_scope=[str(root)]
  p=B.plan(prospective)
  blockers=[v for v in p['blockers'] if not (source['kind'].startswith('legacy_') and v=='store_project_scope_invalid')]
  if blockers:return {'status':'blocked','blockers':blockers,'changesApplied':0}
 request={'project':str(root),'settingsSha256':source['sha256'],'collections':owners,
  'model':model,'dimension':dimension,'backend':args.qmd_backend or ('cpu' if args.qmd_cpu else bootstrap['request']['backend'] if selected else 'auto'),
  'qmdEntry':args.qmd_entry,'node':args.node}
 for key in ('qmdEntry','node'):
  if request[key]:request[key+'Sha256']=C.file_sha(request[key])
 return {'status':'ready_to_install','request':request,'requestSha256':C.digest(request),
  'migrationScope':'preserving_common_file_reindex','changesApplied':0,
  'storeSelected':bool(selected),'laya':'deferred_no_learning'}

def _copy_settings(root,row,auto):
 source=row['source'];path=root/'.auto-context/settings.json'
 if Path(source['path']).is_symlink():raise ValueError('unsafe_install_settings_source')
 if C.file_sha(source['path'])!=source['sha256']:raise ValueError('install_settings_source_changed')
 if path.exists() or path.is_symlink():
  if path.is_symlink() or not path.is_file() or C.file_sha(path)!=row['request']['settingsSha256']:raise ValueError('modern_settings_conflict')
  return
 if not source['kind'].startswith('legacy_'):raise ValueError('settings_missing')
 row['settingsCopyIntent']=source['sha256'];_save(auto,row)
 body=Path(source['path']).read_bytes()
 fd,name=tempfile.mkstemp(prefix='.shared-install-settings-',dir=auto)
 try:
  os.fchmod(fd,0o600)
  row['settingsCopyInode']=os.fstat(fd).st_ino;_save(auto,row)
  with os.fdopen(fd,'wb') as f:f.write(body);f.flush();os.fsync(f.fileno())
  os.link(name,path,follow_symlinks=False)
 finally:Path(name).unlink(missing_ok=True)
 row['settingsCopied']=True;_save(auto,row)

def _scope(root,request):
 settings=root/'.auto-context/settings.json'
 if settings.is_symlink() or not settings.is_file():raise ValueError('unsafe_install_settings')
 cfg=config.normalize_config(json.loads(settings.read_text()))
 if S._collection_owners(root,cfg)!=request['collections']:raise ValueError('install_scope_changed_requires_review')
 return cfg

def _pin(root,request):
 cfg=_scope(root,request)
 if C.file_sha(root/'.auto-context/settings.json')!=request['settingsSha256']:raise ValueError('install_settings_changed_requires_review')
 return cfg

def _expected_queue(selected):
 expected={}
 for scope in selected.get('approvedProjects',{}):
  found=config.find_project_config(scope)
  if found['projectRoot']!=scope:raise ValueError('approved_project_config_changed')
  for name,v in S._collection_owners(scope,found['config']).items():
   if name in expected and expected[name]!=(v['path'],scope):raise ValueError('queue_owner_collision')
   expected[name]=(v['path'],scope)
 return expected

def _environment(selected):
 env={**os.environ,'QMD_BACKEND_MANAGER':str(ROOT/'core/backend_manager.sh'),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
 for key in ('INDEX_PATH','QMD_CONFIG_DIR','XDG_CACHE_HOME','QMD_BIN','QMD_NODE_BIN','QMD_EMBED_MODEL','QMD_GENERATE_MODEL','QMD_RERANK_MODEL'):env.pop(key,None)
 return env

def _drain(selected,auto,deadline):
 queue=dirty_queue.queue_path();env=_environment(selected)
 lockbase=Path(env.get('QMD_LOCK_BASE') or Path(env.get('TMPDIR') or '/tmp')/('qmd-auto-context-locks-'+getpass.getuser()))
 locks=[(k,Path(env.get(var) or lockbase/name)) for k,var,name in
  [('worker','QMD_INDEX_WORKER_LOCKDIR','qmd-index-worker.lock.d'),('writer','QMD_WRITER_LOCKDIR','qmd-update.lock.d'),('embed','QMD_EMBED_LOCKDIR','qmd-embed.lock.d')]]
 def worker(until):
  log=auto/f'install-worker-{os.getpid()}-{time.time_ns()}.log'
  run=maintenance.run_process(['bash',str(ROOT/'backend/index_worker.sh')],cwd=ROOT,env=env,output=log,deadline=until)
  return run['returncode']
 return maintenance.drain(queue,_expected_queue(selected),run=worker,
  lease=lambda:maintenance.locks_status(locks,alive=E.alive),deadline=deadline)

def _source_snapshot(root,request,deadline):
 _pin(root,request);result={}
 for name,owner in request['collections'].items():
  if not owner['indexed']:continue
  base=Path(owner['path']);files={};result[name]=files
  if not base.is_dir() or base.is_symlink():raise ValueError('install_collection_root_missing_or_changed')
  def failed(exc):raise maintenance.UnstableSourceSnapshot('source_scan_changed') from exc
  try:
   for directory,dirs,names in os.walk(base,onerror=failed,followlinks=False):
    for filename in names:
     if not filename.endswith('.md'):continue
     if time.monotonic()>=deadline:raise TimeoutError('install_source_snapshot_deadline')
     path=Path(directory)/filename
     if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent!=base and base in parent.parents):
      raise ValueError('unsafe_install_source_symlink')
     fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
     with os.fdopen(fd,'rb') as handle:
      before=os.fstat(handle.fileno())
      if not stat.S_ISREG(before.st_mode):raise ValueError('unsafe_install_source_file')
      digest=hashlib.sha256()
      for chunk in iter(lambda:handle.read(1024*1024),b''):
       if time.monotonic()>=deadline:raise TimeoutError('install_source_snapshot_deadline')
       digest.update(chunk)
      if not V.same_source_identity(before,os.fstat(handle.fileno())) or not V.same_source_identity(before,path.stat()):
       raise maintenance.UnstableSourceSnapshot('source_changed_while_hashing')
     # Content can return A→B→A after a worker indexed B. Identity/mtime/ctime
     # still show that the supposedly stable indexing window changed.
     files[path.relative_to(base).as_posix()]=V.source_entry(digest.hexdigest(),before)
  except OSError as exc:raise maintenance.UnstableSourceSnapshot('source_scan_changed') from exc
 return result

def _queue_sources(root,row,auto,current):
 owners=row['request']['collections']
 names=V.source_queue_names(row.get('sourceSnapshot',{}),current,row.get('sourceQueueIntent',{}),owners)
 if not names:return
 # Persist intent before enqueue: a crash cannot acknowledge a new snapshot
 # without publishing its normal maintenance work on identical resume.
 row['sourceQueueIntent']={'snapshot':current,'collections':names};_save(auto,row)
 dirty_queue.enqueue_collections({n:owners[n]['path'] for n in names},project_root=str(root))
 row['sourceSnapshot']=current;row['queueCommitted']=True;row.pop('sourceQueueIntent',None);_save(auto,row)

def _vector_scope(selected,request):
 proof=json.loads(S._private_file(S.root()/B.JOURNAL).read_text())['proof']
 return V.vector_scope(proof,request['collections'],request['model'])

def _vector_report(selected,request):
 names,model,fp=_vector_scope(selected,request)
 with sqlite_read.connect(selected['INDEX_PATH']) as db:
  db.execute('BEGIN');vector_integrity.load_vectors(db,R.select())
  return vector_integrity.audit(db,names,model,fp,request['dimension'])

def _ensure_vectors(root,selected,request,row,auto,deadline):
 attempts=0;waits=0
 for _ in range(2):
  report=_vector_report(selected,request)
  if V.vectors_complete(report):return report,attempts,waits
  remaining=deadline-time.monotonic()
  if remaining<=0:raise TimeoutError('physical_vector_repair_deadline')
  with qmd_write_fence.hold(min(30,remaining)) as verify:
   _pin(root,request);verify()
   if S.select()['generation']!=selected['generation']:raise ValueError('vector_repair_store_changed')
   index=Path(selected['INDEX_PATH']);identity=(index.stat().st_dev,index.stat().st_ino)
   db=sqlite3.connect('file:'+quote(str(index),safe='/')+'?mode=rw',uri=True,timeout=.5)
   try:
    try:db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as e:raise TimeoutError('physical_vector_writer_busy') from e
    if (index.stat().st_dev,index.stat().st_ino)!=identity:raise ValueError('vector_repair_index_changed')
    vector_integrity.load_vectors(db,R.select());names,model,fp=_vector_scope(selected,request)
    report=vector_integrity.audit(db,names,model,fp,request['dimension'])
    if V.vectors_complete(report):
     db.rollback();return report,attempts,waits
    row['vectorRepair']=V.vector_repair_intent(selected['generation'],model,fp,report);_save(auto,row)
    vector_integrity.invalidate(db,names,model,fp,request['dimension']);_pin(root,request);verify();db.commit()
    row['vectorRepair']['phase']='metadata_invalidated';_save(auto,row)
   finally:
    if db.in_transaction:db.rollback()
    db.close()
   dirty_queue.enqueue_collections(V.indexed_paths(request['collections']),project_root=str(root))
   row['vectorRepair']['phase']='enqueued';_save(auto,row)
  result=_drain(selected,auto,deadline);attempts+=result['attempts'];waits+=result['waits']
  if result['status']=='deferred':raise TimeoutError('physical_vector_maintenance_deferred:'+result['reason'])
 report=V.require_complete_vectors(_vector_report(selected,request),'physical_vectors_still_incomplete')
 return report,attempts,waits

def _readiness(root,selected,request,deadline):
 if time.monotonic()>=deadline:raise TimeoutError('install_readiness_deadline')
 _pin(root,request)
 if B._model(request['model']['path'])!=request['model']:raise ValueError('embedding_model_changed_requires_review')
 endpoint=E.read()
 if not E.healthy(endpoint['port'],endpoint['url']):raise TimeoutError('install_endpoint_not_ready')
 route=qmd_route.project_paths(root,isolated=True,reject_conflicts=True)
 if route['generation']!=selected['generation'] or setup_guard.status(root)['status']!='ready':raise ValueError('install_hook_route_not_ready')
 runtime=R.select();env={**_environment(selected),**{k:selected[k] for k in ('INDEX_PATH','QMD_CONFIG_DIR','XDG_CACHE_HOME')}}
 V.require_complete_vectors(_vector_report(selected,request),'physical_vectors_incomplete')
 searches={};counts={}
 with sqlite_read.connect(selected['INDEX_PATH']) as db:
  if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('install_index_integrity_failed')
  for name in V.indexed_names(request['collections']):
   counts[name]=db.execute('SELECT count(*) FROM documents WHERE active=1 AND collection=?',(name,)).fetchone()[0]
 for name,count in counts.items():
  if not count:continue  # Empty collections are proved by initializer/ACK.
  remaining=deadline-time.monotonic()
  if remaining<=0:raise TimeoutError('install_readiness_deadline')
  p=subprocess.run([runtime['wrapper'],'query','vec: installation readiness','--no-rerank','--json','-c',name],cwd=root,env=env,capture_output=True,text=True,timeout=min(120,remaining))
  if p.returncode:raise ValueError('install_scoped_search_failed:'+name)
  searches[name]=V.scoped_search_count(name,json.loads(p.stdout))
 if _selected()['generation']!=selected['generation'] or R.select()['generation']!=runtime['generation']:raise ValueError('install_tuple_changed')
 _pin(root,request)
 if B._model(request['model']['path'])!=request['model']:raise ValueError('embedding_model_changed_requires_review')
 if time.monotonic()>=deadline:raise TimeoutError('install_readiness_deadline')
 current=E.read()
 if current['pid']!=endpoint['pid'] or not E.healthy(current['port'],current['url']):raise TimeoutError('install_endpoint_changed_or_not_ready')
 vector_report=V.require_complete_vectors(_vector_report(selected,request),'physical_vectors_changed_during_readiness')
 return {'guard':'ready','store':selected['generation'],'runtime':runtime['generation'],'endpoint':current['url'],'daemonPid':current['pid'],'quickCheck':'ok','scopedSearchCounts':searches,'documents':counts,'physicalVectorAudit':True,'vectorCoverage':vector_report}

def install(args):
 with install_deadline.operation(args.deadline_seconds) as deadline:
  try:return _install(args,deadline)
  except install_deadline.InstallBusy as exc:
   # The outer project lock was never acquired: do not touch its journal.
   return {'status':'deferred','exitCode':75,'reason':str(exc)}

def _install(args,deadline):
 if not args.approve_project_enrollment or not args.approve_index_execution:raise ValueError('shared_install_review_required')
 for key in ('QMD_SETUP_GUARD_FIXTURE','QMD_SANDBOX','GEMINI_SANDBOX','QMD_QUERY_FIXTURE','NODE_OPTIONS','QMD_NO_RELOAD'):
  if os.environ.get(key):raise ValueError('install_inherited_override_rejected:'+key)
 if not math.isfinite(args.deadline_seconds) or not 0<args.deadline_seconds<=3600:raise ValueError('invalid_install_deadline')
 E.inspection_available()  # Denial precedes settings/intent writes.
 proposal=plan(args)
 if proposal['status']!='ready_to_install':return proposal
 if not _selected() and not args.approve_store_activation:raise ValueError('store_activation_review_required')
 root=C.project_root(args.project)
 with C._locked(root) as auto:
  old=C._journal(auto)
  if old and old['phase'] not in ('activated','rolled_back'):raise ValueError('existing_setup_transition_requires_review')
  row=_journal(auto)
  if row:
   if row['requestSha256']!=proposal['requestSha256']:
    old_request=dict(row['request']);new_request=dict(proposal['request'])
    old_request.pop('settingsSha256');new_request.pop('settingsSha256')
    if row['phase']!='completed' or old_request!=new_request:raise ValueError('different_shared_install_request_requires_review')
    # Harmless completed-install query preferences do not alter approved paths.
    row['request']=proposal['request'];row['requestSha256']=proposal['requestSha256'];row['source']=C._config_source(root);_save(auto,row)
  else:
   row={'schema':SCHEMA,'request':proposal['request'],'requestSha256':proposal['requestSha256'],
    'source':C._config_source(root),'phase':'preparing'};_save(auto,row)
  try:
   _copy_settings(root,row,auto)
   _pin(root,row['request'])
   selected=_selected()
   if not selected:
    step=copy.copy(args);step.store_scope=[str(root)]
    prepared=B.prepare(step)
    if prepared['status']=='deferred':raise TimeoutError(prepared['reason'])
    if prepared['status']!='prepared_inactive':raise ValueError('store_prepare_incomplete:'+prepared.get('reason',prepared['status']))
    _pin(root,row['request'])
    activated=B.activate(step)
    if activated['status'] not in ('activated','activated_unchanged'):raise ValueError('store_activation_incomplete:'+activated['status'])
    selected=S.select()
   if row.get('store') and row['store']!=selected['generation']:raise ValueError('install_store_changed')
   row['store']=selected['generation'];row['phase']='enrolling';_save(auto,row)
   _pin(root,row['request'])
   B.enroll(args,expected_settings_sha256=row['request']['settingsSha256'])
   S.register_config(root,_pin(root,row['request']))
   row['phase']='indexing';_save(auto,row)
   _pin(root,row['request'])
   E.ensure()  # Uses canonical owned endpoint, no restart/foreign adoption.
   selected=S.select();sources=[None];totals={'attempts':0,'waits':0};ready={}
   def snapshot_sources():
    sources[0]=_source_snapshot(root,row['request'],deadline);return sources[0]
   def drain_once():
    _queue_sources(root,row,auto,sources[0]);drained=_drain(selected,auto,deadline)
    totals['attempts']+=drained['attempts'];totals['waits']+=drained['waits'];return drained
   def verify_sources():
    report,attempts,waits=_ensure_vectors(root,selected,row['request'],row,auto,deadline)
    totals['attempts']+=attempts;totals['waits']+=waits
    ready.clear();ready.update(_readiness(root,selected,row['request'],deadline))
   stable=maintenance.stable(snapshot_sources,drain_once,
    lambda:_queue_sources(root,row,auto,sources[0]),verify_sources,deadline=deadline)
   if stable['status']=='deferred':
    row['phase']='deferred';row['lastError']=stable['reason'];_save(auto,row);return {**stable,'journal':str(auto/JOURNAL)}
   if maintenance.snapshot(dirty_queue.queue_path(),_expected_queue(selected))['pending']:
    row['phase']='deferred';_save(auto,row);return {'status':'deferred','exitCode':75,'reason':'concurrent_index_work'}
   row['phase']='completed';row['readiness']=ready;row['completedAt']=time.time();_save(auto,row)
   components=setup_readiness.components(True)
   return {**components,'status':'completed' if components['overallReady'] else 'qmd_ready','completionScope':'overall' if components['overallReady'] else 'qmd','exitCode':0,'readiness':ready,'journal':str(auto/JOURNAL),'workerAttempts':totals['attempts'],'busyWaits':totals['waits'],'sourceStabilityPasses':stable['passes'],'laya':'deferred_no_learning','originalsPreserved':True}
  except (TimeoutError,subprocess.TimeoutExpired,maintenance.QueueBusy) as exc:
   row['phase']='deferred';row['lastError']=str(exc);_save(auto,row)
   return {'status':'deferred','exitCode':75,'reason':str(exc),'journal':str(auto/JOURNAL)}
  except Exception as exc:
   # Never undo a selected store or an enrollment that may already be writing.
   selected=_selected()
   if not selected and row.get('settingsCopyIntent'):
    p=root/'.auto-context/settings.json'
    if p.is_file() and not p.is_symlink() and p.stat().st_ino==row.get('settingsCopyInode') and C.file_sha(p)==row['settingsCopyIntent']:p.unlink()
   row['phase']='recovery_required' if selected else 'failed_preparing';row['lastError']=str(exc);_save(auto,row)
   return {'status':row['phase'],'reason':str(exc),'changesApplied':0,'journal':str(auto/JOURNAL),'operatingRollback':False}
