"""Read-only update compatibility checks and inactive shadow-index preparation.

A changed embedding model or vector format is rebuilt in a separate SQLite DB.
No user weights, gold labels, history, original DB, or active pointer is edited.
An explicit later cutover must verify the new index and its project routing.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import uuid
import tempfile

import sqlite_read

REQUIRED_TABLES={'documents','content','content_vectors','store_collections'}
WIKI_SCHEMA_VERSION=2
LABEL_SCHEMA='qmd-compact-wiki-selection-v1'
GOLD_SCHEMA=1
TRAINING_SCHEMA=1


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def inspect_index(index, *, include_file_hash=True):
    """Inspect an existing DB without allowing SQLite to create or write it."""
    path=Path(index)
    if path.is_symlink() or not path.is_file():return {'status':'missing_index'}
    with sqlite_read.connect(path) as db:
        rows=db.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table','view')").fetchall()
        schema={name:sql for name,sql in rows}
        if not REQUIRED_TABLES<=set(schema):return {'status':'incompatible_schema'}
        columns={row[1] for row in db.execute('PRAGMA table_info(content_vectors)')}
        if not {'hash','seq','model','embed_fingerprint'}<=columns:
            return {'status':'incompatible_schema'}
        vector_sql=schema.get('vectors_vec','') or ''
        match=re.search(r'float\[(\d+)\]',vector_sql)
        models=db.execute('SELECT DISTINCT model, embed_fingerprint FROM content_vectors').fetchall()
        count=db.execute('SELECT COUNT(*) FROM documents WHERE active=1').fetchone()[0]
        return {'status':'readable','userVersion':db.execute('PRAGMA user_version').fetchone()[0],
            'schemaSha256':hashlib.sha256(json.dumps({name:schema[name] for name in
                sorted(REQUIRED_TABLES)},sort_keys=True).encode()).hexdigest(),
            'vectorFormat':'vec0-cosine' if 'distance_metric=cosine' in vector_sql else 'unknown',
            'dimension':int(match[1]) if match else None,
            'modelFingerprints':sorted([{'model':name,'fingerprint':fingerprint}
                for name,fingerprint in models],key=lambda row:(row['model'],row['fingerprint'])),
            'activeDocuments':count,'bytes':path.stat().st_size,
            'fileSha256':_sha(path) if include_file_hash else None}


def plan_update(current,target):
    """Return only necessary staged work; never choose a destructive fallback."""
    if not isinstance(current,dict) or not isinstance(target,dict):
        raise ValueError('invalid_update_inventory')
    actions=[];holds=[]
    qmd=current.get('qmd',{})
    if qmd.get('version')!=target.get('qmdVersion') or not set(target.get('qmdCapabilities',[]))<=set(qmd.get('capabilities',[])):
        actions.append('stage_qmd_runtime')
    index=current.get('index',{})
    if (index.get('status')!='readable' or index.get('schemaSha256')!=target.get('indexSchemaSha256')
            or index.get('dimension')!=target.get('embeddingDimension')
            or index.get('vectorFormat')!=target.get('vectorFormat')
            or index.get('modelFingerprints')!=target.get('modelFingerprints')):
        actions.append('stage_shadow_index_and_reembed')
    if set(current.get('wikiVersions',[]))!={WIKI_SCHEMA_VERSION}:
        holds.append('wiki_version_requires_preserving_migration')
    laya=current.get('laya',{})
    if laya.get('runtimeIdentitySha256')!=target.get('layaRuntimeIdentitySha256'):
        actions.append('stage_laya_runtime_and_synthetic_probe')
    if any(laya.get(key)!=target.get(key) for key in
           ('checkpointArchitecture','tokenizerSha256','labelSchema')):
        holds.append('preserve_incompatible_checkpoint')
    if current.get('goldSchema')!=GOLD_SCHEMA or current.get('trainingSchema')!=TRAINING_SCHEMA:
        holds.append('preserve_incompatible_gold_and_training_history')
    return {'status':'review_required' if holds else 'staged_changes_required' if actions else 'compatible',
            'actions':actions,'holds':holds,'preserve':['weights','gold','history','original_index'],
            'requiresCutoverProof':bool(actions)}


def stage_shadow_index(project_root, *, qmd_bin, config_file, wiki_dir, model_cache,
                       expected_model, expected_dimension, allow_execution=False,
                       runner=subprocess.run, bootstrap_empty=False):
    """Build a fresh index while preserving every existing DB and pointer."""
    if not allow_execution:raise ValueError('migration_execution_not_enabled')
    from runtime_work import guarded_run
    def run_stage(command,**options):return guarded_run(command,execute=runner,**options)
    root=Path(project_root).resolve()
    import wiki_topical as topical
    if not topical.opted_in(root):raise ValueError('project_optin_required')
    config=Path(config_file).resolve();wiki=Path(wiki_dir).resolve()
    cache=Path(model_cache).resolve();qmd=Path(qmd_bin).resolve()
    if (root not in config.parents or root not in wiki.parents or not wiki.is_dir() or
            not config.is_file() or not cache.is_dir() or not qmd.is_file()):
        raise ValueError('unsafe_migration_input')
    if not isinstance(expected_model,str) or not expected_model or not isinstance(expected_dimension,int) or expected_dimension<1:
        raise ValueError('invalid_migration_target')
    old=os.environ.get('INDEX_PATH')
    if bootstrap_empty and old:
        raise ValueError('empty_bootstrap_source_index_forbidden')
    old_bytes=Path(old).stat().st_size if old and Path(old).is_file() else 0
    needed=max(512*1024*1024,old_bytes*2)
    if shutil.disk_usage(root).free<needed:raise ValueError('insufficient_migration_disk')
    base=root/'.auto-context/qmd-index-generations'
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    generation=base/('g-'+uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    stage_config=generation/'config';stage_config.mkdir(mode=0o700)
    (stage_config/'index.yml').write_bytes(config.read_bytes())
    stage_cache=generation/'cache';stage_cache.mkdir(mode=0o700)
    # A compatible, verified model cache can be reused without duplicating
    # model weights. Other caches stay in the private generation.
    models=cache/'qmd/models'
    if models.is_dir():
        (stage_cache/'qmd').mkdir(mode=0o700)
        (stage_cache/'qmd/models').symlink_to(models,target_is_directory=True)
    index=generation/'index.sqlite'
    env={**os.environ,'INDEX_PATH':str(index),'QMD_CONFIG_DIR':str(stage_config),
         'XDG_CACHE_HOME':str(stage_cache),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    for command in ([str(qmd),'update'],[str(qmd),'embed','--max-docs-per-batch','1','--max-batch-mb','1']):
        result=run_stage(command,cwd=root,env=env,capture_output=True,text=True,timeout=900,check=False)
        if result.returncode:raise ValueError('shadow_index_build_failed')
    probe_models=[]
    if bootstrap_empty:
        if inspect_index(index).get('activeDocuments') != 0:
            raise ValueError('empty_bootstrap_wiki_changed')
        probe=generation/'bootstrap-probe';probe.mkdir(mode=0o700)
        (probe/'probe.md').write_text('Synthetic local bootstrap vector proof.\n')
        name='qmd-bootstrap-probe-'+uuid.uuid4().hex
        try:
            for command in ([str(qmd),'collection','add',str(probe),'--name',name,'--mask','*.md'],
                            [str(qmd),'embed','--max-docs-per-batch','1','--max-batch-mb','1']):
                result=run_stage(command,cwd=root,env=env,capture_output=True,text=True,timeout=900,check=False)
                if result.returncode:raise ValueError('bootstrap_vector_probe_failed')
            verified=inspect_index(index)
            probe_models=verified.get('modelFingerprints',[])
            if (verified.get('activeDocuments') != 1 or
                    verified.get('dimension') != expected_dimension or
                    verified.get('vectorFormat') != 'vec0-cosine' or
                    len(probe_models) != 1 or probe_models[0]['model'] != expected_model):
                raise ValueError('bootstrap_vector_probe_failed')
            for command in ([str(qmd),'collection','remove',name],[str(qmd),'cleanup']):
                result=run_stage(command,cwd=root,env=env,capture_output=True,text=True,timeout=900,check=False)
                if result.returncode:raise ValueError('bootstrap_probe_cleanup_failed')
        finally:
            (probe/'probe.md').unlink(missing_ok=True)
            (stage_config/'index.yml').write_bytes(config.read_bytes())
    search=run_stage([str(qmd),'vsearch','synthetic compatibility probe','--format','json'],
                  cwd=root,env=env,capture_output=True,text=True,timeout=120,check=False)
    try:
        hits=json.loads(search.stdout)
    except (ValueError,TypeError):
        hits=None
    if search.returncode or not isinstance(hits,list):
        raise ValueError('shadow_search_probe_failed')
    inspected=inspect_index(index)
    if (inspected.get('status')!='readable' or
            inspected['dimension']!=expected_dimension or
            inspected['vectorFormat']!='vec0-cosine' or
            (bootstrap_empty and (inspected['activeDocuments']!=0 or
                                  inspected['modelFingerprints'])) or
            (not bootstrap_empty and (inspected['activeDocuments']<1 or
                not any(row['model']==expected_model for row in inspected['modelFingerprints'])))):
        raise ValueError('shadow_index_probe_failed')
    if bootstrap_empty:
        with sqlite_read.connect(index) as db:
            if (db.execute('SELECT 1 FROM documents LIMIT 1').fetchone() or
                    db.execute('SELECT 1 FROM content_vectors LIMIT 1').fetchone()):
                raise ValueError('bootstrap_probe_cleanup_failed')
    prepared={'schema':'qmd-shadow-index-v1','index':str(index),
              'inspection':inspected,'sourceIndex':old,
              'sourceIndexFingerprint':sqlite_read.snapshot_fingerprint(old) if old else None}
    if bootstrap_empty:
        prepared['bootstrapModelFingerprints']=probe_models
    prepared['configSha256'] = _sha(stage_config/'index.yml')
    path=generation/'prepared.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(prepared,out,sort_keys=True);out.write('\n');out.flush();os.fsync(out.fileno())
    return {'status':'staged_inactive','generation':str(generation),
            'index':str(index),'inspection':inspected}


def _prepared_shadow(project_root,generation):
    root=Path(project_root).resolve();generation=Path(generation)
    if generation.parent!=root/'.auto-context/qmd-index-generations' or generation.is_symlink():
        raise ValueError('shadow_generation_outside_project')
    path=generation/'prepared.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('shadow_generation_unprepared')
    value=json.loads(path.read_text())
    if value.get('schema')!='qmd-shadow-index-v1' or value.get('index')!=str(generation/'index.sqlite'):
        raise ValueError('shadow_generation_invalid')
    if inspect_index(value['index'])!=value['inspection']:
        raise ValueError('shadow_index_changed')
    source=value.get('sourceIndex')
    if source is not None and (not isinstance(value.get('sourceIndexFingerprint'),dict) or
                              sqlite_read.snapshot_fingerprint(source)!=value['sourceIndexFingerprint']):
        raise ValueError('source_index_changed_during_migration')
    return value


def activate_shadow_index(project_root,generation):
    """Cut over only the project-local index pointer; old DB remains untouched."""
    root=Path(project_root).resolve()
    generation=Path(generation)
    import wiki_topical as topical
    if not topical.opted_in(root):raise ValueError('project_optin_required')
    pointer=root/'.auto-context/qmd-index-active.json'
    lock=root/'.auto-context/.qmd-index-pointer.lock'
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if os.fstat(fd).st_uid!=os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ValueError('unsafe_index_pointer_lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        prepared=_prepared_shadow(root,generation)
        source=prepared['sourceIndex']
        guard=(sqlite_read.cutover_guard(source,prepared['sourceIndexFingerprint'])
               if source is not None else nullcontext())
        wrote=False;previous=None
        try:
            with guard:
                if pointer.is_symlink():raise ValueError('unsafe_index_pointer')
                previous=json.loads(pointer.read_text()) if pointer.is_file() else None
                if previous is not None: select_runtime(root)
                value={'schema':'qmd-index-pointer-v1','generation':str(generation),
                       'index':prepared['index'], 'indexSha256':prepared['inspection']['fileSha256'],
                       'preparedSha256':_sha(generation/'prepared.json'),
                       'previous':previous if previous is not None else
                           {'index':source,'indexFingerprint':prepared['sourceIndexFingerprint']}}
                _write_pointer(pointer,value);wrote=True
        except BaseException:
            if wrote:
                if previous is None:pointer.unlink()
                else:_write_pointer(pointer,previous)
            raise
    finally:
        os.close(fd)
    return {'status':'index_selected','index':prepared['index']}


def _write_pointer(pointer,value):
    fd,temp=tempfile.mkstemp(prefix='.index-pointer-',dir=pointer.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,sort_keys=True);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,pointer)
    finally:
        Path(temp).unlink(missing_ok=True)


def select_runtime(project_root):
    """Return project-local QMD env or None; a bad pointer never falls back."""
    root=Path(project_root).resolve()
    if not (root/'.auto-context/settings.json').is_file():
        import config as qmd_config
        found=Path(qmd_config.find_project_config(str(root)).get('projectRoot',root)).resolve()
        if found!=root:
            root=found
        else:
            # A damaged settings file must not hide an ancestor's active
            # pointer and send an update into the global index.
            home=Path.home().resolve()
            for candidate in (root,*root.parents):
                if (candidate/'.auto-context/qmd-index-active.json').exists() or (candidate/'.auto-context/qmd-index-active.json').is_symlink():
                    root=candidate
                    break
                if candidate==home:break
    pointer=root/'.auto-context/qmd-index-active.json'
    if pointer.is_symlink():raise ValueError('unsafe_index_pointer')
    if not pointer.exists():return None
    info=pointer.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077 or info.st_size>65536:
        raise ValueError('unsafe_index_pointer')
    value=json.loads(pointer.read_text())
    if not isinstance(value,dict):raise ValueError('index_pointer_changed')
    generation=Path(value.get('generation',''))
    base=root/'.auto-context/qmd-index-generations'
    index=generation/'index.sqlite'
    prepared_path=generation/'prepared.json'
    if (value.get('schema')!='qmd-index-pointer-v1' or generation.parent!=base or
            generation.is_symlink() or value.get('index')!=str(index) or
            prepared_path.is_symlink() or not prepared_path.is_file() or
            _sha(prepared_path)!=value.get('preparedSha256')):
        raise ValueError('index_pointer_changed')
    prepared=json.loads(prepared_path.read_text())
    config=generation/'config/index.yml';cache=generation/'cache'
    if (prepared.get('schema')!='qmd-shadow-index-v1' or
            prepared.get('index')!=str(index) or config.is_symlink() or
            not config.is_file() or _sha(config)!=prepared.get('configSha256') or
            cache.is_symlink() or not cache.is_dir() or index.is_symlink()):
        raise ValueError('index_pointer_changed')
    inspection=inspect_index(index,include_file_hash=False)
    original=prepared.get('inspection')
    # QMD cleanup removes the final embedding row when the last document is
    # retired. The selected DB then has no row from which to read the staged
    # model fingerprint. Its pinned config and schema still identify the
    # generation; retain the fingerprint check whenever any vector remains.
    model_fingerprints={(row['model'],row['fingerprint'])
                        for row in inspection.get('modelFingerprints',[])}
    expected_fingerprints={(row['model'],row['fingerprint'])
                           for row in (prepared.get('bootstrapModelFingerprints') or
                                       original.get('modelFingerprints',[]))} if isinstance(original,dict) else set()
    empty_after_retirement=(inspection.get('activeDocuments')==0 and not model_fingerprints)
    if (inspection.get('status')!='readable' or not isinstance(original,dict) or
            any(inspection.get(key)!=original.get(key) for key in
                ('schemaSha256','dimension','vectorFormat')) or
            not (expected_fingerprints<=model_fingerprints or empty_after_retirement)):
        raise ValueError('index_pointer_changed')
    return {'INDEX_PATH':str(index),'QMD_CONFIG_DIR':str(config.parent),
            'XDG_CACHE_HOME':str(cache),'generation':str(generation)}


def select_index(project_root):
    selected=select_runtime(project_root)
    if selected is None:raise ValueError('index_pointer_missing')
    return Path(selected['INDEX_PATH'])


def rollback_shadow_index(project_root):
    root=Path(project_root).resolve();pointer=root/'.auto-context/qmd-index-active.json'
    lock=root/'.auto-context/.qmd-index-pointer.lock'
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if os.fstat(fd).st_uid!=os.getuid() or os.fstat(fd).st_mode & 0o077:
            raise ValueError('unsafe_index_pointer_lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        selected=select_runtime(root)
        if selected is None:raise ValueError('index_pointer_missing')
        current=json.loads(pointer.read_text())
        previous=current.get('previous')
        if not isinstance(previous,dict):raise ValueError('index_rollback_unavailable')
        if previous.get('schema')=='qmd-index-pointer-v1':
            _write_pointer(pointer,previous)
            restored=select_runtime(root)
            return {'status':'index_rolled_back','index':restored['INDEX_PATH']}
        source=previous.get('index')
        guard=(sqlite_read.cutover_guard(source,previous.get('indexFingerprint'))
               if source is not None else nullcontext())
        unlinked=False
        try:
            with guard:
                if source is not None and inspect_index(source)['status']!='readable':
                    raise ValueError('source_index_changed_during_migration')
                pointer.unlink();unlinked=True
        except BaseException:
            if unlinked:_write_pointer(pointer,current)
            raise
        return {'status':'index_rolled_back_original','index':source}
    finally:
        os.close(fd)


if __name__=='__main__':
    import sys
    if len(sys.argv)!=3 or sys.argv[1]!='resolve-env':raise SystemExit(2)
    try:
        from qmd_route import project_paths, PATH_KEYS
        route=project_paths(sys.argv[2])
        if route['selected']:
            values=[route[key] for key in PATH_KEYS]
            if any(any(char in value for char in ('\t','\n','\r')) for value in values):
                raise ValueError('unsafe_index_path_control_character')
            print('\t'.join(values))
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
        raise SystemExit(1)
