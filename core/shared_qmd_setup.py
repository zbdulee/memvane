"""Explicit new-machine initialization; preparation never selects a pointer.

Reuse a proved local QMD and local GGUF, index one synthetic file, prove its
embedding and search, and remove all probe records before an approved cutover.
The user's global DB/config and existing plugin generations are never migrated.
"""
from contextlib import contextmanager
import fcntl
import install_deadline
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import uuid
import re

import install_update as coordinator
import qmd_runtime
import qmd_shared_store as store
import runtime_update
import sqlite_read
import qmd_endpoint
import qmd_write_fence

SCHEMA = 'qmd-shared-setup-v1'
JOURNAL = 'bootstrap-journal.json'
REPLACEMENT_JOURNAL = 'replacement-bootstrap-journal.json'
JOURNALS = frozenset((JOURNAL, REPLACEMENT_JOURNAL))

def _scopes(values):
    rows={}
    for value in values:
        scope=coordinator.project_root(value);settings=scope/'.auto-context/settings.json'
        if settings.is_symlink() or not settings.is_file():raise ValueError('store_project_scope_invalid')
        import config as qmd_config
        found=qmd_config.find_project_config(scope)
        if found['projectRoot']!=str(scope) or found['config'].get('indexing') is not True:
            raise ValueError('store_project_scope_invalid')
        rows[str(scope)]=coordinator.file_sha(settings)
    return rows


def _model(value):
    path = Path(value) if value else None
    if (path is None or not path.is_absolute() or path.is_symlink() or
            not path.is_file() or path.stat().st_uid not in (0, os.getuid()) or
            path.stat().st_mode & 0o022):
        raise ValueError('local_embedding_model_required')
    with path.open('rb') as source:
        if source.read(4) != b'GGUF':
            raise ValueError('local_embedding_model_required')
    return {'path': str(path.resolve()), 'sha256': coordinator.file_sha(path)}


def _private_snapshot(path):
    """One bounded private-file identity; an atomic same-byte replacement differs."""
    path = store._private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    try:
        before = os.fstat(fd)
        if before.st_size > 65536: raise ValueError('replacement_pointer_too_large')
        raw = os.read(fd, before.st_size + 1)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode,
                                 item.st_nlink, item.st_size, item.st_mtime_ns,
                                 item.st_ctime_ns)
        if (len(raw) != before.st_size or
                identity(before) != identity(after) or
                identity(before) != identity(current)):
            raise ValueError('replacement_pointer_changed')
        return {'sha256': hashlib.sha256(raw).hexdigest(),
                'dev': before.st_dev, 'ino': before.st_ino,
                'bytes': before.st_size, 'mtimeNs': before.st_mtime_ns,
                'ctimeNs': before.st_ctime_ns}, raw
    finally:
        os.close(fd)


def _selected_replacement():
    """Pin selected store/runtime without writing their pointers or generation."""
    base = store._private_dir(store._unlinked_root())
    pointer, raw = _private_snapshot(base / 'active.json')
    selected = store.select()
    if _private_snapshot(base / 'active.json')[0] != pointer:
        raise ValueError('replacement_store_pointer_changed')
    generation = Path(selected['generation'])
    if generation.name != json.loads(raw)['generation']:
        raise ValueError('replacement_store_pointer_changed')
    config = Path(selected['QMD_CONFIG_DIR']) / 'index.yml'
    config_identity, _ = _private_snapshot(config)
    index_info = runtime_update.inspect_index(selected['INDEX_PATH'], include_file_hash=False)
    if (index_info.get('status') != 'readable' or
            type(index_info.get('dimension')) is not int):
        raise ValueError('replacement_selected_dimension_unproved')
    from setup_entry import _configured_model
    embed_id = _configured_model(config)
    if (not isinstance(embed_id, str) or not embed_id or
            _private_snapshot(config)[0] != config_identity):
        raise ValueError('replacement_selected_model_unproved')
    runtime_root = qmd_runtime._private(qmd_runtime.managed_root())
    runtime_pointer, runtime_raw = _private_snapshot(runtime_root / 'active.json')
    runtime = qmd_runtime.select()
    if _private_snapshot(runtime_root / 'active.json')[0] != runtime_pointer:
        raise ValueError('replacement_runtime_pointer_changed')
    if runtime['generation'] != json.loads(runtime_raw)['generation']:
        raise ValueError('replacement_runtime_pointer_changed')
    if runtime['proof'].get('version') != qmd_runtime.VERSION:
        raise ValueError('replacement_managed_qmd_incompatible')
    return {'generation': str(generation), 'pointer': pointer,
            'configSha256': config_identity['sha256'], 'embedModelId': embed_id,
            'dimension': index_info['dimension'],
            'runtimeGeneration': runtime['generation'],
            'runtimePointer': runtime_pointer, 'runtimeProof': runtime['proof'],
            'runtimeWrapper': runtime['wrapper']}


def _model_identity(path):
    if not isinstance(path, str):
        raise ValueError('local_embedding_model_required')
    source = Path(path)
    if not source.is_absolute() or str(source.resolve(strict=False)) != str(source):
        raise ValueError('local_embedding_model_required')
    try: before = source.lstat()
    except OSError as exc: raise ValueError('local_embedding_model_required') from exc
    model = _model(str(source))
    after = source.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mode,
                             item.st_nlink, item.st_size, item.st_mtime_ns,
                             item.st_ctime_ns)
    if identity(before) != identity(after):
        raise ValueError('store_embedding_model_changed')
    return model, {'dev': before.st_dev, 'ino': before.st_ino,
                   'bytes': before.st_size, 'mtimeNs': before.st_mtime_ns,
                   'ctimeNs': before.st_ctime_ns}


def _model_matches_selected(path, embed_id):
    if Path(embed_id).is_absolute():
        return path == embed_id
    basename = embed_id.rsplit('/', 1)[-1]
    names = {basename}
    if embed_id.startswith('hf:'):
        owner = embed_id[3:].split('/', 1)[0]
        names.add('hf_' + owner + '_' + basename)
    return Path(path).name in names


def plan(args, *, replacement=False):
    inventory = coordinator.inspect(args.project)
    blockers = []
    backend=getattr(args,'qmd_backend',None) or ('cpu' if args.qmd_cpu else None)
    if backend is None:blockers.append('qmd_backend_selection_required')
    if args.qmd_cpu and backend!='cpu':blockers.append('qmd_backend_selection_conflict')
    if args.source_index or args.reviewed_v2_mappings or args.bootstrap_empty_wiki:
        blockers.append('store_initializer_does_not_migrate_project_data')
    if not coordinator.supported_platform(): blockers.append('unsupported_managed_platform')
    base = store._unlinked_root()
    if base.exists(): store._private_dir(base)
    old_selection = None
    if replacement:
        try: old_selection = _selected_replacement()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            blockers.append('replacement_selected_store_unavailable:' + str(exc))
    elif (base / 'active.json').exists() or (base / 'active.json').is_symlink():
        blockers.append('plugin_qmd_store_already_selected')
    mode = args.qmd_mode
    if mode == 'auto': mode = 'active' if inventory['managedQmd'] else 'reuse'
    if replacement and mode != 'active': blockers.append('replacement_selected_managed_qmd_required')
    qmd = None
    try:
        if mode == 'active':
            active = qmd_runtime.select()
            prepared=qmd_runtime._prepared(active['generation'])
            if backend=='cpu' and not prepared.get('forceCpu'):
                raise ValueError('cpu_store_requires_cpu_runtime')
            if backend=='cpu' and not prepared.get('directCli'):
                raise ValueError('cpu_store_requires_direct_cli_runtime')
            if backend=='auto' and (prepared.get('forceCpu') or not prepared.get('nativeNoBuild')):
                raise ValueError('auto_store_requires_no_build_runtime')
            qmd = {'mode': 'active', 'generation': active['generation'], 'proof': active['proof']}
        elif mode == 'reuse' and not replacement:
            entry = args.qmd_entry or shutil.which('qmd')
            node = args.node or inventory['candidateExecutables']['node']
            if not entry or not node: raise ValueError('qmd_entry_and_node_required')
            qmd = {'mode': 'reuse', 'proof': qmd_runtime._probe(entry, node)}
        else:
            raise ValueError('store_setup_requires_existing_qmd')
    except (OSError, ValueError) as exc: blockers.append(str(exc))
    if (replacement and qmd is not None and old_selection is not None and
            (qmd['generation'] != old_selection['runtimeGeneration'] or
             qmd['proof'] != old_selection['runtimeProof'])):
        blockers.append('replacement_managed_qmd_changed')
    model_identity = None
    try:
        if replacement:
            model, model_identity = _model_identity(args.embedding_model_file)
        else:
            model = _model(args.embedding_model_file)
    except (OSError, ValueError) as exc:
        blockers.append(str(exc)); model = None
    if replacement:
        expected_sha = getattr(args, 'expected_embedding_model_sha256', None)
        expected_bytes = getattr(args, 'expected_embedding_model_bytes', None)
        if (type(expected_sha) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha) or
                type(expected_bytes) is not int or expected_bytes < 1):
            blockers.append('replacement_model_identity_required')
        elif model is not None and (model['sha256'] != expected_sha or
                                    model_identity['bytes'] != expected_bytes):
            blockers.append('replacement_model_identity_mismatch')
        if (model is not None and old_selection is not None and
                not _model_matches_selected(model['path'], old_selection['embedModelId'])):
            blockers.append('replacement_model_identifier_mismatch')
        if old_selection is not None and args.expected_dimension != old_selection['dimension']:
            blockers.append('replacement_selected_dimension_mismatch')
        if args.expected_dimension != 768:
            blockers.append('replacement_dimension_768_required')
    if type(args.expected_dimension) is not int or args.expected_dimension < 1:
        blockers.append('expected_embedding_dimension_required')
    if not args.approve_index_execution: blockers.append('index_execution_approval_required')
    try:scopes=_scopes(getattr(args,'store_scope',None) or [inventory['project']])
    except (OSError,ValueError) as exc:blockers.append(str(exc));scopes={}
    request = {'schema': SCHEMA, 'project': inventory['project'], 'backend': backend,
               'qmd': qmd, 'model': model, 'dimension': args.expected_dimension,'scopes':scopes}
    if replacement:
        request.update(replacementFrom=old_selection, modelIdentity=model_identity,
                       expectedModelSha256=getattr(args, 'expected_embedding_model_sha256', None),
                       expectedModelBytes=getattr(args, 'expected_embedding_model_bytes', None))
    result = {'status': 'blocked' if blockers else 'ready_to_prepare',
            'blockers': sorted(set(blockers)), 'request': request,
            'requestSha256': coordinator.digest(request),
            'storeRoot': str(base), 'changesApplied': 0,
            'migrationScope': ('inactive_proven_replacement_no_data_migration'
                               if replacement else 'new_machine_empty_plugin_store_no_user_data_migration')}
    if replacement: result['cutoverReady'] = False
    return result


@contextmanager
def _locked():
    base = store._unlinked_root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    store._private_dir(base)
    fd = os.open(base / 'bootstrap.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('unsafe_store_setup_lock')
        install_deadline.flock(fd, fcntl.LOCK_EX)
        yield base
    finally: os.close(fd)


def _journal(base, name=JOURNAL):
    if name not in JOURNALS: raise ValueError('invalid_store_setup_journal_name')
    path = base / name
    if path.is_symlink(): raise ValueError('invalid_store_setup_journal')
    if not path.exists(): return None
    store._private_file(path)
    if path.stat().st_size > 65536: raise ValueError('invalid_store_setup_journal')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('invalid_store_setup_journal')
    return value


def _save(base, journal, name=JOURNAL):
    if name not in JOURNALS: raise ValueError('invalid_store_setup_journal_name')
    if (journal.get('operation') == 'replacement') != (name == REPLACEMENT_JOURNAL):
        raise ValueError('store_setup_journal_operation_mismatch')
    coordinator._atomic(base / name, journal)
    fd=os.open(base,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


@contextmanager
def _native_lease(base):
    # The open description is inherited by the exec-based native runner. A
    # stopped controller cannot free this lease while that writer is alive.
    fd=os.open(base/'bootstrap-native.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_nlink!=1 or st.st_mode&0o077:
            raise ValueError('unsafe_bootstrap_native_lease')
        install_deadline.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield fd
    finally:os.close(fd)


def _check_prepared(journal):
    request = journal['request']
    if request.get('scopes') is not None and _scopes(request['scopes'])!=request['scopes']:
        raise ValueError('store_project_scope_changed')
    if _model(request['model']['path']) != request['model']:
        raise ValueError('store_embedding_model_changed')
    runtime = qmd_runtime._prepared(journal['runtime']['generation'])
    if runtime['proof'] != request['qmd']['proof']:
        raise ValueError('store_qmd_runtime_changed')
    if request['backend'] == 'cpu' and runtime.get('forceCpu') is not True:
        raise ValueError('store_qmd_backend_changed')
    if request['backend']=='cpu' and not runtime.get('directCli'):
        raise ValueError('store_qmd_direct_cli_required')
    if request['backend']=='auto' and (runtime.get('forceCpu') or not runtime.get('nativeNoBuild')):
        raise ValueError('store_qmd_backend_changed')
    generation = Path(journal['generation'])
    if generation.parent != store.root() / 'generations': raise ValueError('unsafe_store_generation')
    store._private_dir(generation)
    for name in ('prepared.json', 'bootstrap-proof.json', 'index.sqlite', 'config/index.yml'):
        store._private_file(generation / name)
    proof = json.loads((generation / 'bootstrap-proof.json').read_text())
    if proof != journal['proof']: raise ValueError('store_preparation_changed')
    expected = journal['files']
    for name, sha in expected.items():
        if coordinator.file_sha(generation / name) != sha: raise ValueError('store_preparation_changed')
    info = runtime_update.inspect_index(generation / 'index.sqlite', include_file_hash=False)
    if info.get('status') != 'readable' or info['dimension'] != request['dimension'] or info['vectorFormat'] != 'vec0-cosine':
        raise ValueError('store_index_schema_changed')
    with sqlite_read.connect(generation / 'index.sqlite') as db:
        if any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in runtime_update.REQUIRED_TABLES):
            raise ValueError('plugin_qmd_generation_not_empty')
    return generation


def _native_receipts(stderr):
    rows=[]
    for line in stderr.splitlines():
        try:row=json.loads(line)
        except (ValueError,TypeError):continue
        if isinstance(row,dict) and row.get('schema')=='qmd-native-no-build-v1':rows.append(row)
    return rows


def _build(journal, base, native_lease=None, *, journal_name):
    import vector_integrity
    generations=base/'generations';generations.mkdir(mode=0o700,exist_ok=True);store._private_dir(generations)
    progress=journal.get('building')
    resuming = progress is not None
    if resuming and (not isinstance(progress, dict) or not progress):
        raise ValueError('bootstrap_checkpoint_changed')
    if progress:
        if journal.get('buildingSha256')!=coordinator.digest(progress) or progress['requestSha256']!=journal['requestSha256']:
            raise ValueError('bootstrap_checkpoint_changed')
        generation=Path(progress['generation'])
        if generation.parent!=generations or not generation.name.startswith('g-'):raise ValueError('bootstrap_checkpoint_outside_root')
    else:
        generation=generations/('g-'+uuid.uuid4().hex)
        progress={'generation':str(generation),'requestSha256':journal['requestSha256'],'name':'qmd-bootstrap-'+uuid.uuid4().hex,'done':[],'inFlight':None,'nativeReceipts':[]}
    if (journal_name == REPLACEMENT_JOURNAL and
            (str(generation) == journal['request']['replacementFrom']['generation'] or
             not re.fullmatch(r'g-[0-9a-f]{32}', generation.name))):
        raise ValueError('replacement_generation_collides_with_selected')
    if journal_name == REPLACEMENT_JOURNAL and not resuming and (
            generation.exists() or generation.is_symlink()):
        raise ValueError('replacement_generation_already_exists')
    if not resuming:
        journal['building']=progress
    def save_progress():
        journal['buildingSha256']=coordinator.digest(progress)
        if journal_name == JOURNAL:
            _save(base, journal)
        else:
            _save(base, journal, journal_name)
    save_progress()  # Generation identity precedes all stage writes.
    generation.mkdir(mode=0o700,exist_ok=resuming);store._private_dir(generation)
    config=generation/'config';cache=generation/'cache';probe=generation/'bootstrap-probe'
    for directory in (config,cache,probe):directory.mkdir(mode=0o700,exist_ok=True);store._private_dir(directory)
    request=journal['request'];cfg=config/'index.yml';index=generation/'index.sqlite';name=progress['name']
    if not cfg.exists():
        if progress['done']:raise ValueError('bootstrap_checkpoint_config_missing')
        cfg.write_bytes((json.dumps({'collections':{},'models':{'embed':request['model']['path']}},sort_keys=True)+'\n').encode());cfg.chmod(0o600)
    store._private_file(cfg)
    if not progress['inFlight']:
        for relative,digest in progress.get('files',{}).items():
            file=generation/relative
            if file.is_symlink() or not file.is_file() or coordinator.file_sha(file)!=digest:raise ValueError('bootstrap_checkpoint_file_changed')
    paper=probe/'probe.md';body=b'Synthetic local bootstrap vector proof.\n'
    if paper.exists() or paper.is_symlink():
        if paper.is_symlink() or paper.read_bytes()!=body:raise ValueError('bootstrap_probe_source_changed')
    else:paper.write_bytes(body);paper.chmod(0o600)
    env={**os.environ,'INDEX_PATH':str(index),'QMD_CONFIG_DIR':str(config),'XDG_CACHE_HOME':str(cache),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    for key in ('NODE_OPTIONS','QMD_EMBED_MODEL','QMD_GENERATE_MODEL','QMD_RERANK_MODEL'):env.pop(key,None)
    if request['backend']=='cpu':env['QMD_FORCE_CPU']='1'
    else:env.pop('QMD_FORCE_CPU',None);env.pop('QMD_LLAMA_GPU',None)
    native_receipts=progress['nativeReceipts']
    def step(label,args,verify=None):
        if label in progress['done']:return
        progress['inFlight']=label;save_progress()
        result=subprocess.run([journal['runtime']['wrapper'],*args],env=env,cwd=generation,capture_output=True,text=True,timeout=120,check=False,
                              pass_fds=(() if native_lease is None else (native_lease,)))
        if result.returncode:
            # Lost add checkpoint: only the exact synthetic owner is reusable.
            same=False
            if label=='add' and index.is_file():
                with sqlite_read.connect(index) as db:
                    row=db.execute('SELECT path,pattern FROM store_collections WHERE name=?',(name,)).fetchone()
                    same=row==(str(probe),'*.md')
            if label=='remove' and index.is_file():
                # An interrupted successful remove is reusable only in this
                # pinned generation with the exact synthetic source retained,
                # an empty collection config and no surviving owner/doc row.
                from setup_entry import _configured_model
                with sqlite_read.connect(index) as db:
                    same=(not db.execute('SELECT 1 FROM store_collections LIMIT 1').fetchone()
                          and not db.execute('SELECT 1 FROM documents LIMIT 1').fetchone()
                          and cfg.read_bytes().startswith(b'collections: {}\n')
                          and _configured_model(cfg)==request['model']['path'])
            if not same:
                coordinator._atomic(generation/'probe-failure.json',{'command':list(args),'returncode':result.returncode,'stderr':result.stderr[-8192:]})
                raise ValueError('plugin_qmd_bootstrap_probe_failed')
        native_receipts.extend(_native_receipts(result.stderr))
        if verify:verify(result)
        progress['done'].append(label);progress['inFlight']=None
        progress['files']={rel:coordinator.file_sha(generation/rel) for rel in ('index.sqlite','config/index.yml') if (generation/rel).is_file()}
        save_progress()
    def embedding_proof(_):
        info=runtime_update.inspect_index(index,include_file_hash=False);fingerprints=info.get('modelFingerprints',[])
        if (info.get('status')!='readable' or info['activeDocuments']!=1 or info['dimension']!=request['dimension'] or info['vectorFormat']!='vec0-cosine' or len(fingerprints)!=1 or fingerprints[0]['model']!=request['model']['path'] or not fingerprints[0]['fingerprint']):raise ValueError('plugin_qmd_bootstrap_vector_invalid')
        with sqlite_read.connect(index) as db:
            vector_integrity.load_vectors(db,qmd_runtime._prepared(journal['runtime']['generation']))
            report=vector_integrity.audit(db,[name],fingerprints[0]['model'],fingerprints[0]['fingerprint'],request['dimension'])
        if report['defects']:raise ValueError('bootstrap_physical_vectors_incomplete')
        progress['fingerprints']=fingerprints
    def query_proof(result):
        hits=json.loads(result.stdout)
        if not isinstance(hits,list) or not any(isinstance(h,dict) and h.get('file')=='qmd://'+name+'/probe.md' for h in hits):raise ValueError('plugin_qmd_bootstrap_search_failed')
    step('update',['update'])
    step('add',['collection','add',str(probe),'--name',name,'--mask','*.md'])
    # On an uncheckpointed embed retry, remove only defective synthetic-probe
    # metadata, never healthy completed rows or another generation's data.
    if 'embed' not in progress['done'] and index.is_file():
        info=runtime_update.inspect_index(index,include_file_hash=False);fp=info.get('modelFingerprints',[])
        if len(fp)==1 and fp[0]['model']==request['model']['path']:
            import sqlite3
            db=sqlite3.connect(index)
            try:
                db.execute('BEGIN IMMEDIATE');vector_integrity.load_vectors(db,qmd_runtime._prepared(journal['runtime']['generation']))
                vector_integrity.invalidate(db,[name],fp[0]['model'],fp[0]['fingerprint'],request['dimension']);db.commit()
            finally:db.close()
    step('embed',['embed','--max-docs-per-batch','1','--max-batch-mb','1'],embedding_proof)
    step('query',['query','vec: synthetic local bootstrap vector proof','--no-rerank','--json','-c',name],query_proof)
    step('remove',['collection','remove',name]);step('cleanup',['cleanup'])
    from setup_entry import _configured_model
    if not cfg.read_bytes().startswith(b'collections: {}\n') or _configured_model(cfg)!=request['model']['path']:raise ValueError('plugin_qmd_bootstrap_config_invalid')
    paper.unlink(missing_ok=True)
    fingerprints=progress['fingerprints']
    if _model(request['model']['path']) != request['model']:
        raise ValueError('store_embedding_model_changed')
    info = runtime_update.inspect_index(index, include_file_hash=False)
    with sqlite_read.connect(index) as db:
        if any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in runtime_update.REQUIRED_TABLES):
            raise ValueError('plugin_qmd_bootstrap_cleanup_failed')
    if info['dimension'] != request['dimension'] or info['vectorFormat'] != 'vec0-cosine':
        raise ValueError('store_index_schema_changed')
    if request['backend']=='auto' and (not native_receipts or any(
            row.get('build')!='never' or row.get('skipDownload') is not True or
            row.get('gpu') not in ('metal','cuda','vulkan',False) for row in native_receipts)):
        raise ValueError('plugin_qmd_native_policy_unproved')
    index.chmod(0o600)
    prepared = {'schema': store.SCHEMA, 'generation': generation.name,
                'configSha256': coordinator.file_sha(cfg), 'indexSchemaSha256': info['schemaSha256']}
    policy={'schema':'qmd-shared-scope-v1','projects':request['scopes']}
    coordinator._atomic(generation/'scope-policy.json',policy)
    prepared['scopePolicySha256']=coordinator.file_sha(generation/'scope-policy.json')
    coordinator._atomic(generation / 'prepared.json', prepared)
    proof = {'schema': SCHEMA, 'modelPath': request['model']['path'],
             'modelSha256': request['model']['sha256'], 'dimension': request['dimension'],
             'modelFingerprints': fingerprints, 'searchProved': True}
    if request['backend']=='auto':proof.update(nativeBackend=native_receipts[0]['gpu'],nativeNoBuild=True)
    coordinator._atomic(generation / 'bootstrap-proof.json', proof)
    return {'generation': str(generation), 'proof': proof,
            'files': {name: coordinator.file_sha(generation / name) for name in
                      ('index.sqlite', 'config/index.yml', 'prepared.json', 'bootstrap-proof.json','scope-policy.json')}}


def prepare(args):
    proposal = plan(args)
    if proposal['status'] != 'ready_to_prepare': return proposal
    with _locked() as base:
        if (base / 'active.json').exists(): raise ValueError('plugin_qmd_store_already_selected')
        journal = _journal(base)
        if journal and journal['requestSha256'] != proposal['requestSha256']:
            if not getattr(args,'replace_inactive_store_preparation',False):
                raise ValueError('store_setup_request_changed')
            runtime_pointer=qmd_runtime.managed_root()/'active.json'
            if journal['phase']!='prepared_inactive' or runtime_pointer.exists() or runtime_pointer.is_symlink():
                raise ValueError('store_setup_replacement_not_inactive')
            _check_prepared(journal)
            archive=base/'journal-archive';archive.mkdir(mode=0o700,exist_ok=True);store._private_dir(archive)
            saved=archive/(journal['requestSha256']+'.json')
            current=(base/JOURNAL).read_bytes()
            if saved.exists():
                store._private_file(saved)
                if saved.read_bytes()!=current:raise ValueError('store_setup_archive_conflict')
            else:
                fd=os.open(saved,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as output:output.write(current);output.flush();os.fsync(output.fileno())
                directory=os.open(archive,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
            journal=None  # Retain every prior generation; only the journal advances.
        if not journal:
            journal = {'schema': SCHEMA, 'request': proposal['request'],
                       'requestSha256': proposal['requestSha256'], 'phase': 'preparing'}
            _save(base, journal)
        if journal['phase'] == 'prepared_inactive':
            _check_prepared(journal)
            return {'status': journal['phase'], 'generation': journal['generation'], 'changesApplied': 0}
        try:
            if 'runtime' not in journal:
                qmd = journal['request']['qmd']
                if qmd['mode'] == 'active':
                    runtime = qmd_runtime.select()
                else:
                    runtime = qmd_runtime.prepare_existing(qmd_runtime.managed_root(),
                        qmd['proof']['qmd'], qmd['proof']['node'],
                        force_cpu=journal['request']['backend'] == 'cpu',
                        native_no_build=journal['request']['backend']=='auto')
                journal['runtime'] = {'generation': runtime['generation'], 'wrapper': runtime['wrapper']}
                _save(base, journal)
            runtime = qmd_runtime._prepared(journal['runtime']['generation'])
            if runtime['proof'] != journal['request']['qmd']['proof']:
                raise ValueError('store_qmd_runtime_changed')
            with _native_lease(base) as lease:
                journal.update(_build(journal, base, lease, journal_name=JOURNAL))
            _check_prepared(journal)
            journal['phase'] = 'prepared_inactive'; journal['lastError'] = None
            _save(base, journal)
            return {'status': journal['phase'], 'generation': journal['generation'],
                    'proof': journal['proof'], 'changesApplied': 0}
        except BlockingIOError:
            return {'status':'deferred','reason':'bootstrap_native_writer_busy','exitCode':75,'changesApplied':0}
        except install_deadline.InstallBusy as exc:
            # Retain preparation receipts for the identical approved retry.
            journal['lastError'] = str(exc); _save(base, journal)
            return {'status':'deferred','reason':str(exc),'exitCode':75}
        except Exception as exc:
            journal['phase'] = 'failed_preparing'; journal['lastError'] = str(exc); _save(base, journal)
            return {'status': 'failed_preparing', 'reason': str(exc), 'changesApplied': 0}


def plan_replacement(args):
    """Read-only proposal for a new proved generation beside a selected store."""
    return plan(args, replacement=True)


def _check_replacement_sources(request):
    if _selected_replacement() != request['replacementFrom']:
        raise ValueError('replacement_selected_store_changed')
    model, identity = _model_identity(request['model']['path'])
    if model != request['model'] or identity != request['modelIdentity']:
        raise ValueError('replacement_embedding_model_changed')
    if _scopes(request['scopes']) != request['scopes']:
        raise ValueError('replacement_project_scope_changed')


def _archive_replacement_journal(base, journal, selected_generation):
    """Retain an obsolete attempt and every generation before a new request."""
    if (journal.get('operation') != 'replacement' or
            journal.get('phase') not in ('preparing', 'failed_preparing',
                                         'prepared_inactive_replacement') or
            type(journal.get('requestSha256')) is not str or
            not re.fullmatch(r'[0-9a-f]{64}', journal['requestSha256'])):
        raise ValueError('replacement_journal_invalid')
    generations = base / 'generations'
    building = journal.get('building')
    if building is not None and not isinstance(building, dict):
        raise ValueError('replacement_journal_invalid')
    for value in (journal.get('generation'),
                  (building or {}).get('generation')):
        if value is None: continue
        if type(value) is not str: raise ValueError('replacement_journal_invalid')
        path = Path(value)
        if (path.parent != generations or
                not re.fullmatch(r'g-[0-9a-f]{32}', path.name) or
                path.name == Path(selected_generation).name):
            raise ValueError('replacement_journal_generation_unsafe')
    source = store._private_file(base / REPLACEMENT_JOURNAL)
    raw = source.read_bytes()
    if len(raw) > 65536: raise ValueError('replacement_journal_invalid')
    archive = base / 'journal-archive'
    archive.mkdir(mode=0o700, exist_ok=True)
    store._private_dir(archive)
    saved = archive / ('replacement-' + hashlib.sha256(raw).hexdigest() + '.json')
    if saved.exists() or saved.is_symlink():
        store._private_file(saved)
        if saved.read_bytes() != raw: raise ValueError('replacement_journal_archive_conflict')
    else:
        fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        directory = os.open(archive, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    return str(saved)


def prepare_replacement(args):
    """Prove only an inactive empty generation; never select or migrate it."""
    proposal = plan_replacement(args)
    if proposal['status'] != 'ready_to_prepare': return proposal
    accepted = getattr(args, 'accepted_replacement_request_sha256', None)
    if (type(accepted) is not str or not re.fullmatch(r'[0-9a-f]{64}', accepted) or
            accepted != proposal['requestSha256']):
        raise ValueError('replacement_request_acceptance_required')
    with _locked() as base:
        current = plan_replacement(args)
        if current['status'] != 'ready_to_prepare' or current['requestSha256'] != accepted:
            raise ValueError('replacement_request_changed')
        request = current['request']
        _check_replacement_sources(request)
        journal = _journal(base, REPLACEMENT_JOURNAL)
        if journal:
            changed = (journal.get('operation') != 'replacement' or
                       journal.get('requestSha256') != accepted or
                       journal.get('request') != request)
            restart_failed = (journal.get('phase') == 'failed_preparing' and
                              getattr(args, 'replace_inactive_store_preparation', False))
            if changed or restart_failed:
                if not getattr(args, 'replace_inactive_store_preparation', False):
                    raise ValueError('replacement_request_changed')
                _archive_replacement_journal(base, journal,
                                             request['replacementFrom']['generation'])
                journal = None
        if not journal:
            selected_runtime = qmd_runtime.select()
            if (selected_runtime['generation'] != request['qmd']['generation'] or
                    selected_runtime['proof'] != request['qmd']['proof'] or
                    selected_runtime['wrapper'] != request['replacementFrom']['runtimeWrapper']):
                raise ValueError('replacement_managed_qmd_changed')
            journal = {'schema': SCHEMA, 'operation': 'replacement',
                       'request': request, 'requestSha256': accepted,
                       'phase': 'preparing',
                       'runtime': {'generation': request['qmd']['generation'],
                                   'wrapper': selected_runtime['wrapper']}}
            _save(base, journal, REPLACEMENT_JOURNAL)
        if journal['phase'] == 'prepared_inactive_replacement':
            _check_prepared(journal)
            _check_replacement_sources(request)
            return {'status': 'prepared_inactive_replacement',
                    'generation': journal['generation'], 'cutoverReady': False,
                    'changesApplied': 0}
        if journal['phase'] not in ('preparing', 'failed_preparing'):
            raise ValueError('replacement_journal_phase_invalid')
        try:
            runtime = qmd_runtime.select()
            if (runtime['generation'] != request['qmd']['generation'] or
                    runtime['proof'] != request['qmd']['proof'] or
                    runtime['wrapper'] != journal['runtime']['wrapper']):
                raise ValueError('replacement_managed_qmd_changed')
            with _native_lease(base) as lease:
                journal.update(_build(journal, base, lease,
                                      journal_name=REPLACEMENT_JOURNAL))
            _check_replacement_sources(request)
            _check_prepared(journal)
            _check_replacement_sources(request)
            journal['phase'] = 'prepared_inactive_replacement'
            journal['lastError'] = None
            _save(base, journal, REPLACEMENT_JOURNAL)
            return {'status': journal['phase'], 'generation': journal['generation'],
                    'proof': journal['proof'], 'cutoverReady': False,
                    'changesApplied': 0}
        except BlockingIOError:
            return {'status': 'deferred', 'reason': 'bootstrap_native_writer_busy',
                    'exitCode': 75, 'cutoverReady': False, 'changesApplied': 0}
        except install_deadline.InstallBusy as exc:
            journal['lastError'] = str(exc)
            _save(base, journal, REPLACEMENT_JOURNAL)
            return {'status': 'deferred', 'reason': str(exc), 'exitCode': 75,
                    'cutoverReady': False, 'changesApplied': 0}
        except Exception as exc:
            journal['phase'] = 'failed_preparing'
            journal['lastError'] = str(exc)
            _save(base, journal, REPLACEMENT_JOURNAL)
            return {'status': 'failed_preparing', 'reason': str(exc),
                    'cutoverReady': False, 'changesApplied': 0}


def replacement_status():
    """Read-only record of an inactive proof; never implies cutover authority."""
    base = store._unlinked_root()
    if not base.exists():
        return {'status': 'not_prepared', 'verification': 'journal_record_only',
                'cutoverReady': False, 'changesApplied': 0}
    store._private_dir(base)
    journal = _journal(base, REPLACEMENT_JOURNAL)
    if not journal:
        return {'status': 'not_prepared', 'verification': 'journal_record_only',
                'cutoverReady': False, 'changesApplied': 0}
    if journal.get('operation') != 'replacement':
        raise ValueError('replacement_journal_invalid')
    return {'status': journal['phase'], 'generation': journal.get('generation'),
            'lastError': journal.get('lastError'),
            'requestSha256': journal.get('requestSha256'),
            'verification': 'journal_record_only', 'cutoverReady': False,
            'changesApplied': 0}


def replacement_model_evidence():
    """Re-prove an inactive replacement for a Laya provider review.

    This is a read-only snapshot, not authority to select the replacement QMD
    generation. A caller must recheck it when preparing or activating its own
    candidate; the old shared-store pointer remains selected throughout.
    """
    base = store._private_dir(store._unlinked_root())
    journal_path = base / REPLACEMENT_JOURNAL
    journal_identity, _ = _private_snapshot(journal_path)
    journal = _journal(base, REPLACEMENT_JOURNAL)
    if (journal is None or journal.get('operation') != 'replacement' or
            journal.get('phase') != 'prepared_inactive_replacement'):
        raise ValueError('replacement_model_proof_not_prepared')
    request = journal.get('request')
    if (not isinstance(request, dict) or
            journal.get('requestSha256') != coordinator.digest(request) or
            any(not isinstance(request.get(key), dict) for key in
                ('replacementFrom', 'model', 'qmd', 'scopes')) or
            any(not isinstance(journal.get(key), dict) for key in
                ('runtime', 'proof', 'files'))):
        raise ValueError('replacement_model_proof_invalid')
    generation = _check_prepared(journal)
    _check_replacement_sources(request)
    model, model_identity = _model_identity(request['model']['path'])
    if (model != request['model'] or model_identity != request['modelIdentity'] or
            model['sha256'] != request['expectedModelSha256'] or
            model_identity['bytes'] != request['expectedModelBytes'] or
            journal['proof'].get('schema') != SCHEMA or
            journal['proof'].get('modelPath') != model['path'] or
            journal['proof'].get('modelSha256') != model['sha256'] or
            journal['proof'].get('dimension') != request['dimension'] or
            journal['proof'].get('searchProved') is not True or
            str(generation) == request['replacementFrom']['generation']):
        raise ValueError('replacement_model_proof_invalid')
    selected = _selected_replacement()
    if selected != request['replacementFrom']:
        raise ValueError('replacement_selected_store_changed')
    if (request['qmd'].get('mode') != 'active' or
            request['qmd'].get('generation') != selected['runtimeGeneration'] or
            request['qmd'].get('proof') != selected['runtimeProof'] or
            journal['runtime'].get('generation') != selected['runtimeGeneration'] or
            journal['runtime'].get('wrapper') != selected['runtimeWrapper'] or
            request['dimension'] != selected['dimension'] or
            request['dimension'] != 768 or
            not _model_matches_selected(model['path'], selected['embedModelId'])):
        raise ValueError('replacement_model_source_mismatch')
    if _private_snapshot(journal_path)[0] != journal_identity:
        raise ValueError('replacement_model_proof_changed')
    if _selected_replacement() != selected:
        raise ValueError('replacement_selected_store_changed')
    if _model_identity(model['path']) != (model, model_identity):
        raise ValueError('replacement_embedding_model_changed')
    evidence = {
        'schema': 'qmd-inactive-replacement-model-evidence-v1',
        'generation': str(generation),
        'requestSha256': journal['requestSha256'],
        'bootstrapProofSha256': journal['files']['bootstrap-proof.json'],
        'modelPath': model['path'], 'modelSha256': model['sha256'],
        'modelBytes': model_identity['bytes'],
        'modelIdentity': model_identity,
        'dimension': request['dimension'],
        'legacyGeneration': selected['generation'],
        'legacyIndexPath': str(Path(selected['generation']) / 'index.sqlite'),
        'sourceDb': str(Path(selected['generation']) / 'index.sqlite'),
        'legacyPointer': selected['pointer'],
        'runtimeGeneration': selected['runtimeGeneration'],
        'runtimePointer': selected['runtimePointer'],
        'runtimeProofSha256': coordinator.digest(selected['runtimeProof']),
        'scopeSha256': coordinator.digest(request['scopes']),
        'approvedProjectSettings': dict(request['scopes']),
    }
    return {**evidence, 'evidenceSha256': coordinator.digest(evidence)}


def status():
    base = store._unlinked_root()
    if not base.exists(): return {'status': 'not_prepared', 'changesApplied': 0}
    store._private_dir(base)
    journal = _journal(base)
    if not journal: return {'status': 'not_prepared', 'changesApplied': 0}
    return {'status': journal['phase'], 'generation': journal.get('generation'),
            'lastError': journal.get('lastError'), 'changesApplied': 0}


def plan_legacy_enrollment(args):
    """Review a new owner of an old selected store without granting it.

    The old generation has no bootstrap/model proof. This action never
    supplies one, changes the selected pointer, or writes a QMD collection.
    It requires every current physical collection to have an exact registered
    owner so a new project's names cannot silently take an old row.
    """
    import config as qmd_config
    from laya_qmd_migration import QMD_253, inspect_qmd_collections
    base=store._private_dir(store._unlinked_root())
    selected=store.select()
    if selected.get('approvedProjects') is not None:
        raise ValueError('legacy_unscoped_store_required')
    pinned=_selected_replacement()
    project=coordinator.project_root(args.project)
    settings=project/'.auto-context/settings.json'
    if settings.is_symlink() or not settings.is_file():
        raise ValueError('canonical_settings_required')
    found=qmd_config.find_project_config(str(project))
    if (found['projectRoot']!=str(project) or found['configPath']!=str(settings)
            or found['config'].get('indexing') is not True):
        raise ValueError('canonical_indexed_settings_required')
    wanted=store._collection_owners(project,found['config'])
    if not wanted or not all(row['indexed'] for row in wanted.values()):
        raise ValueError('indexed_collections_required')
    registry,identity=store._registry_with_identity(base)
    if identity is None or any(row['scope']==str(project) for row in registry.values()):
        raise ValueError('legacy_owner_already_registered')
    if set(wanted)&set(registry):
        raise ValueError('plugin_qmd_collection_collision')
    for scope in sorted({row['scope'] for row in registry.values()}):
        previous=qmd_config.find_project_config(scope)
        if (previous['projectRoot']!=scope or
                {name:row for name,row in registry.items() if row['scope']==scope}
                != store._collection_owners(scope,previous['config'])):
            raise ValueError('legacy_owner_scope_unproved')
    with sqlite_read.connect(selected['INDEX_PATH']) as db:
        physical={name:(path,pattern) for name,path,pattern in db.execute(
            'SELECT name,path,pattern FROM store_collections')}
    if (not physical or len(physical)>16 or
            set(physical)!={name for name,row in registry.items() if row['indexed']} or
            any(physical[name]!=(registry[name]['path'],'**/*.md') for name in physical)):
        raise ValueError('legacy_physical_registry_mismatch')
    inventory=inspect_qmd_collections(selected['INDEX_PATH'],QMD_253,tuple(physical))
    request={'schema':'qmd-legacy-enrollment-request-v1',
             'project':str(project),'settingsSha256':coordinator.file_sha(settings),
             'collectionNames':sorted(wanted),'owners':wanted,
             'generation':selected['generation'],
             'pointerSha256':pinned['pointer']['sha256'],
             'legacySelected':pinned,'registryIdentity':identity,
             'registrySnapshot':registry,
             'physicalNames':sorted(physical),'sourceDigest':inventory['digest'],
             'sourceFormat':QMD_253}
    # QMD's ordinary worker runs a global embed. A source inventory/backup
    # proves preservation, not that this store's existing vectors and the
    # reviewed local GGUF have the same provenance. No queue may be published
    # while that proof is missing.
    bootstrap=_journal(base)
    model_blocker=(None if bootstrap and bootstrap.get('phase')=='activated'
                   and bootstrap.get('generation')==selected['generation']
                   else 'legacy_embedding_model_proof_required')
    return {'status':'review_required','request':request,
            'proposalSha256':coordinator.digest(request),'changesApplied':0,
            'backupRequired':True,'activationBlockers':([model_blocker] if model_blocker else []),
            'modelProof':'legacy_unproved_preserved'}


def _legacy_journal(base):
    path=base/store.LEGACY_ENROLL_JOURNAL
    if not path.exists() and not path.is_symlink():return None
    file=store._private_file(path)
    if file.stat().st_size>65536:raise ValueError('legacy_enrollment_journal_invalid')
    row=json.loads(file.read_text())
    if (not isinstance(row,dict) or row.get('schema')!='qmd-legacy-enrollment-v1'
            or row.get('phase') not in ('prepared','publishing','completed')
            or not isinstance(row.get('request'),dict)
            or row.get('requestSha256')!=coordinator.digest(row['request'])
            or type(row.get('backupVerified')) is not bool
            or row.get('backupPath')!=str(base/('legacy-enrollment-backup-'+
                row['requestSha256'][:24]+'.sqlite'))):
        raise ValueError('legacy_enrollment_journal_invalid')
    return row


def _save_legacy(base,row):
    coordinator._atomic(base/store.LEGACY_ENROLL_JOURNAL,row)
    fd=os.open(base,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def prepare_legacy_enrollment(args):
    """Save an intent and a verified whole-DB backup before any new grant."""
    if not args.approve_project_enrollment:
        raise ValueError('project_enrollment_review_required')
    accepted=getattr(args,'accepted_legacy_enrollment_sha256',None)
    with _locked() as base:
        with qmd_write_fence.hold(args.writer_wait_seconds) as verify_fence:
            proposal=plan_legacy_enrollment(args)
            if proposal['proposalSha256']!=accepted:
                raise ValueError('legacy_enrollment_proposal_changed')
            request=proposal['request'];row=_legacy_journal(base)
            if row and row['requestSha256']!=accepted:
                raise ValueError('legacy_enrollment_request_changed')
            if not row:
                backup=base/('legacy-enrollment-backup-'+accepted[:24]+'.sqlite')
                row={'schema':'qmd-legacy-enrollment-v1','phase':'prepared',
                     'request':request,'requestSha256':accepted,
                     'backupPath':str(backup),'backupVerified':False}
                _save_legacy(base,row)
            else:
                backup=Path(row['backupPath'])
            from laya_qmd_migration import (backup_qmd_collections,
                verify_restore_collections, inspect_qmd_collections)
            names=request['physicalNames']
            if not row['backupVerified']:
                if not backup.exists() and not backup.is_symlink():
                    result=backup_qmd_collections(store.select()['INDEX_PATH'],
                        request['sourceFormat'],names,backup)
                    if result['source_digest']!=request['sourceDigest']:
                        raise ValueError('legacy_enrollment_source_changed')
                elif backup.is_symlink():
                    raise ValueError('legacy_enrollment_backup_changed')
                restored=verify_restore_collections(backup,request['sourceFormat'],
                    names,request['sourceDigest'])
                if restored['verified'] is not True:
                    raise ValueError('legacy_enrollment_restore_failed')
                verify_fence()
                current=inspect_qmd_collections(store.select()['INDEX_PATH'],
                    request['sourceFormat'],names)
                if current['digest']!=request['sourceDigest']:
                    raise ValueError('legacy_enrollment_source_changed')
                row['backupVerified']=True
                _save_legacy(base,row)
            return {'status':'prepared_inactive','journal':str(base/store.LEGACY_ENROLL_JOURNAL),
                    'backup':str(backup),'sourceDigest':request['sourceDigest'],
                    'changesApplied':0,'modelProof':'legacy_unproved_preserved'}


def activate_legacy_enrollment(args):
    """Commit one reviewed new owner; interrupted queueing is idempotent."""
    if not args.approve_project_enrollment or not args.approve_index_execution:
        raise ValueError('project_enrollment_review_required')
    accepted=getattr(args,'accepted_legacy_enrollment_sha256',None)
    with _locked() as base:
        with qmd_write_fence.hold(args.writer_wait_seconds) as verify_fence:
            row=_legacy_journal(base)
            if not row or not row.get('backupVerified') or row['requestSha256']!=accepted:
                raise ValueError('legacy_enrollment_preparation_required')
            request=row['request'];selected=store.select()
            bootstrap=_journal(base)
            if (bootstrap is None or bootstrap.get('phase')!='activated'
                    or bootstrap.get('generation')!=selected['generation']):
                raise ValueError('legacy_embedding_model_proof_required')
            import setup_readiness
            if setup_readiness.qmd_model_reason() is not None:
                raise ValueError('legacy_embedding_model_proof_required')
            proof=bootstrap.get('proof')
            proved=json.loads(store._private_file(Path(selected['generation']) /
                'bootstrap-proof.json').read_text())
            if (not isinstance(proof,dict) or proof!=proved or
                    bootstrap.get('request',{}).get('model') !=
                    {'path':proof.get('modelPath'),'sha256':proof.get('modelSha256')} or
                    type(proof.get('dimension')) is not int or
                    proof['dimension']!=request['legacySelected']['dimension'] or
                    not isinstance(proof.get('modelFingerprints'),list) or
                    len(proof['modelFingerprints'])!=1 or
                    proof['modelFingerprints'][0].get('model')!=proof.get('modelPath')):
                raise ValueError('legacy_embedding_model_proof_required')
            import vector_integrity
            with sqlite_read.connect(selected['INDEX_PATH']) as db:
                db.execute('BEGIN')
                vector_integrity.load_vectors(db,qmd_runtime.select())
                audit=vector_integrity.audit(db,request['physicalNames'],
                    proof['modelPath'],proof['modelFingerprints'][0]['fingerprint'],
                    proof['dimension'])
                db.rollback()
            if audit['defects']:
                raise ValueError('legacy_physical_vectors_model_unproved')
            if (selected['generation']!=request['generation'] or
                    _private_snapshot(base/'active.json')[0]['sha256']!=request['pointerSha256'] or
                    _selected_replacement()!=request['legacySelected']):
                raise ValueError('legacy_enrollment_selected_store_changed')
            from laya_qmd_migration import (verify_restore_collections,
                inspect_qmd_collections)
            verify_restore_collections(row['backupPath'],request['sourceFormat'],
                                       request['physicalNames'],request['sourceDigest'])
            if row['phase']=='prepared':
                current=plan_legacy_enrollment(args)
                if current['proposalSha256']!=accepted:
                    raise ValueError('legacy_enrollment_proposal_changed')
                row['phase']='publishing';_save_legacy(base,row)
            else:
                registry=store._registry(base)
                baseline={name:value for name,value in registry.items()
                          if name not in request['owners']}
                if (baseline!=request['registrySnapshot'] or
                        any(registry.get(name)!=owner for name,owner in request['owners'].items()
                            if name in registry)):
                    raise ValueError('legacy_enrollment_registry_changed')
            verify_fence()
            store._reserve_owners(base,request['owners'])
            registry=store._registry(base)
            if (registry!={**request['registrySnapshot'],**request['owners']}):
                raise ValueError('legacy_enrollment_registry_changed')
            grant={'schema':'qmd-legacy-enrollment-grant-v1',
                   'requestSha256':accepted}
            grant_path=base/store.LEGACY_ENROLL_GRANT
            if grant_path.exists() or grant_path.is_symlink():
                if json.loads(store._private_file(grant_path).read_text())!=grant:
                    raise ValueError('legacy_enrollment_grant_changed')
            else:
                coordinator._atomic(grant_path,grant)
            verify_fence()
            import dirty_queue
            dirty_queue.enqueue_collections({name:owner['path']
                for name,owner in request['owners'].items() if owner['indexed']},
                project_root=request['project'])
            row['phase']='completed';_save_legacy(base,row)
            return {'status':'enrolled','project':request['project'],
                    'collections':request['collectionNames'],'indexStatus':'queued',
                    'backup':row['backupPath'],'changesApplied':1,
                    'modelProof':'legacy_unproved_preserved'}


def activate(args):
    live=getattr(args,'allow_live_host_sessions',False)
    if not args.approve_store_activation or (not live and not args.old_host_sessions_restarted):
        raise ValueError('store_activation_review_required')
    with _locked() as base:
        with qmd_write_fence.hold(getattr(args,'writer_wait_seconds',30)) as verify_fence:
            return _activate_locked(args,base,live,verify_fence)

def _activate_locked(args,base,live,verify_fence):
        journal = _journal(base)
        if not journal: return {'status': 'not_prepared', 'changesApplied': 0}
        if journal['request']['project'] != str(coordinator.project_root(args.project)):
            raise ValueError('store_setup_project_changed')
        if journal['phase'] == 'activated':
            selected = store.select()
            if selected['generation'] != journal['generation']:
                raise ValueError('store_pointer_changed')
            if qmd_runtime.select()['generation'] != journal['runtime']['generation']:
                raise ValueError('store_runtime_pointer_changed')
            return {'status': 'activated_unchanged', 'changesApplied': 0}
        if journal['phase'] in ('activating', 'recovery_required'):
            return {'status': 'recovery_required', 'reason': 'interrupted_store_activation', 'changesApplied': 0}
        if journal['phase'] != 'prepared_inactive': raise ValueError('store_not_prepared')
        generation = _check_prepared(journal)
        if live and not journal['request'].get('scopes'):raise ValueError('live_store_scope_proof_required')
        if coordinator._daemon_identity() is not None: raise ValueError('qmd_daemon_must_be_quiescent')
        # Foreign listeners are handled by bounded actual-bind retries at
        # daemon start. Only our selected daemon must be quiescent at cutover.
        qmd_endpoint.assert_quiescent()
        pointer = base / 'active.json'
        runtime_pointer = qmd_runtime.managed_root() / 'active.json'
        if pointer.exists() or pointer.is_symlink(): raise ValueError('plugin_qmd_store_already_selected')
        qmd = journal['request']['qmd']
        if qmd['mode'] == 'active':
            if qmd_runtime.select()['generation'] != journal['runtime']['generation']:
                raise ValueError('store_runtime_pointer_changed')
        elif runtime_pointer.exists() or runtime_pointer.is_symlink():
            raise ValueError('store_runtime_pointer_changed')
        journal['phase'] = 'activating'; _save(base, journal)
        try:
            verify_fence()
            if qmd['mode'] == 'reuse':
                payload = {'schema': qmd_runtime.SCHEMA, 'generation': journal['runtime']['generation'], 'previous': None}
                encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':'))+'\n').encode()
                journal['runtimePointerSha256'] = hashlib.sha256(encoded).hexdigest(); _save(base, journal)
                qmd_runtime.activate(qmd_runtime.managed_root(), journal['runtime']['generation'], require_unselected=True)
            if qmd_runtime.select()['generation'] != journal['runtime']['generation']:
                raise ValueError('store_runtime_pointer_changed')
            verify_fence()
            store.activate_empty(generation)
            if journal['request'].get('scopes') is not None and _scopes(journal['request']['scopes'])!=journal['request']['scopes']:
                raise ValueError('store_project_scope_changed')
            verify_fence()
            journal['phase'] = 'activated'; _save(base, journal)
            return {'status': 'activated', 'generation': str(generation),
                    'migrationScope': 'empty_plugin_store_no_user_data_migration',
                    'liveHostSessionsAllowed':live,'writerFence':'writer-and-embed-held',
                    'approvedProjects':sorted(journal['request'].get('scopes',{}))}
        except Exception as original:
            # Only the newly selected runtime can be undone here. A selected
            # store may receive writes, so preserve it for explicit recovery.
            if not pointer.exists() and not pointer.is_symlink() and qmd['mode'] == 'reuse' and runtime_pointer.exists():
                try:
                    deselected=qmd_runtime.deselect_if_matches(qmd_runtime.managed_root(), journal.get('runtimePointerSha256'))
                except install_deadline.InstallBusy as exc:
                    journal['phase']='recovery_required';journal['lastError']=str(exc);_save(base,journal)
                    raise ValueError('store_activation_cleanup_busy_requires_recovery') from exc
                if deselected:
                    journal['phase'] = 'prepared_inactive'
                else: journal['phase'] = 'recovery_required'
            elif not pointer.exists() and not pointer.is_symlink() and (
                    qmd['mode'] == 'active' or not runtime_pointer.exists()):
                journal['phase'] = 'prepared_inactive'
            else: journal['phase'] = 'recovery_required'
            _save(base, journal)
            if journal['phase']=='recovery_required' and isinstance(original,TimeoutError):
                raise ValueError('interrupted_store_activation_requires_recovery') from original
            raise


def enroll(args,*,expected_settings_sha256=None):
    """Grant one explicit project, then enqueue normal maintenance, without DB DML.

    The sealed initial policy is retained. A generation-bound receipt persists
    approved intent; queued=false recovers interruption before durable enqueue.
    Ordinary hooks maintain enrolled projects without requesting this again.
    """
    if not getattr(args, 'approve_project_enrollment', False) or not args.approve_index_execution:
        raise ValueError('project_enrollment_review_required')
    scope = str(coordinator.project_root(args.project))
    expected = _scopes([scope])
    if expected_settings_sha256 is not None and expected[scope]!=expected_settings_sha256:
        raise ValueError('store_project_scope_changed_requires_review')
    with _locked() as base:
        with qmd_write_fence.hold(args.writer_wait_seconds) as verify_fence:
            selected = store.select()
            journal = _journal(base)
            if not journal or journal.get('phase') != 'activated' or journal['generation'] != selected['generation']:
                raise ValueError('project_shared_store_transition_pending')
            if qmd_runtime.select()['generation'] != journal['runtime']['generation']:
                raise ValueError('store_runtime_pointer_changed')
            if selected.get('approvedProjects') is None:
                raise ValueError('scoped_store_required_for_enrollment')
            generation = Path(selected['generation'])
            prepared = json.loads(store._private_file(generation/'prepared.json').read_text())
            rows = store._enrolled_projects(generation, prepared)
            if scope in selected['approvedProjects'] and (scope not in rows or rows[scope]['queued']):
                return {'status': 'enrolled_unchanged', 'generation': str(generation), 'project': scope, 'changesApplied': 0}
            import config as qmd_config, resolve_paths, dirty_queue
            config = qmd_config.find_project_config(scope)['config']
            resolved = resolve_paths.resolve_paths(scope, json.dumps(config))
            if resolved.get('refused') or not resolved.get('indexEntries'):
                raise ValueError('plugin_qmd_collection_scope_invalid')
            wanted = store._collection_owners(scope, config)
            if any(any(c in name+owner['path']+scope for c in '\t\r\n') for name, owner in wanted.items()):
                raise ValueError('plugin_qmd_collection_scope_invalid')
            if _scopes([scope]) != expected:
                raise ValueError('store_project_scope_changed')
            verify_fence()
            store._discard_unpublished_reservations(scope)
            store._reserve_owners(base, wanted)
            if _scopes([scope]) != expected:
                raise ValueError('store_project_scope_changed')
            verify_fence()
            receipt = {'schema': 'qmd-shared-enrollment-v1', 'generation': str(generation),
                       'scopePolicySha256': prepared['scopePolicySha256'], 'projects': rows}
            rows[scope] = {'settingsSha256': expected[scope], 'queued': False}
            coordinator._atomic(generation/'enrolled-projects.json', receipt)
            # No pointer, model, wiki, history or original DB changes. Normal
            # worker owns collection registration/update/embed under its locks.
            dirty_queue.enqueue_collections({e['name']: str((Path(scope)/e['path']).resolve())
                for e in resolved['indexEntries']}, project_root=scope)
            rows[scope]['queued'] = True
            coordinator._atomic(generation/'enrolled-projects.json', receipt)
            return {'status': 'enrolled', 'generation': str(generation), 'project': scope,
                    'settingsSha256': expected[scope], 'collections': sorted(e['name'] for e in resolved['indexEntries']),
                    'changesApplied': 1, 'indexStatus': 'queued', 'sessionsTerminated': False}


def run(args):
    with install_deadline.operation(getattr(args,'deadline_seconds',600)):
        coordinator.project_root(args.project)
        try:
            return {'plan-store': lambda: plan(args), 'prepare-store': lambda: prepare(args),
                    'store-status': status, 'activate-store': lambda: activate(args), 'enroll-store': lambda: enroll(args),
                    'plan-replacement-store': lambda: plan_replacement(args),
                    'prepare-replacement-store': lambda: prepare_replacement(args),
                    'replacement-store-status': replacement_status,
                    'plan-legacy-enroll': lambda: plan_legacy_enrollment(args),
                    'prepare-legacy-enroll': lambda: prepare_legacy_enrollment(args),
                    'activate-legacy-enroll': lambda: activate_legacy_enrollment(args)}[args.action]()
        except TimeoutError as exc:
            return {'status':'deferred','reason':str(exc),'exitCode':75}
