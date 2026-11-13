"""Scoped local candidate publication; no training, teacher or DB rollback."""
from copy import deepcopy
from pathlib import Path
import fcntl,json,math,os,re,time
from .dataset import read_private_json
from .local_cycle import _safe_file,_hash,_atomic_json,ResourceDeferred
from .learning_profile import digest
from .resource_coordination import _lock
from .training_cursor import _sync
from . import inference_identity as identity,resident

JOURNAL='candidate-apply.json'
ARCHIVE='candidate-applications'
SCHEMA='qmd-candidate-application-v1'

def put(path,value):
    path=Path(path)
    if any(p.is_symlink() for p in path.parents):raise ValueError('apply_unsafe_path')
    if path.exists() or path.is_symlink():_safe_file(path)
    _atomic_json(path,value);_sync(path.parent)

def read_journal(root):
    row=read_private_json(Path(root)/JOURNAL)
    if (row.get('schema')!=SCHEMA or not re.fullmatch(r'[a-f0-9]{32}',row.get('apply_id',''))
            or row.get('phase') not in ('intent','prepared','pointer_committed','activated','rejected')):
        raise ValueError('apply_journal_invalid')
    return row

def _archive(root,row):
    folder=Path(root)/ARCHIVE
    if not folder.exists():folder.mkdir(mode=0o700)
    resident._private_state(folder)
    path=folder/(row['apply_id']+'.json')
    if path.exists() or path.is_symlink():
        if read_private_json(path)!=row:raise ValueError('apply_archive_changed')
    else:put(path,row)

def readable(root):
    """Never serve the old loaded head under an incomplete new pointer."""
    root=Path(root);pointer=read_private_json(root/'active-checkpoint.json')
    marker=pointer.get('application')
    if marker is None:return
    if (not isinstance(marker,dict) or set(marker)!={'schema','id'} or marker['schema']!=SCHEMA
            or not re.fullmatch(r'[a-f0-9]{32}',marker['id'])):raise ValueError('apply_pointer_invalid')
    row=read_journal(root)
    if row['apply_id']!=marker['id']:
        row=read_private_json(root/ARCHIVE/(marker['id']+'.json'))
    if (row.get('schema')!=SCHEMA or row.get('phase')!='activated' or row.get('new_pointer')!=pointer
            or row.get('new_receipt')!=read_private_json(root/identity.RECEIPT)
            or read_private_json(root/identity.BINDING)!={'schema':identity.SCHEMA,'receipt_sha256':digest(row['new_receipt'])}):
        raise ValueError('apply_activation_pending')

def validate_candidate(root,policy,cycle_id,*,expected=None):
    """Recheck real admission, actual trained batch, fresh disjoint quality."""
    from . import learning_profile
    from .runtime_authorization import require_operation,proposal_sha256
    from .learning_queue import LearningQueue
    from .automatic_cohort import training_and_validation
    from .automatic_staging import _case_evidence,_quality_ready
    from .shared import _db,_case_active,read_only_validation
    root=Path(root)
    if any(p.is_symlink() for p in (root,*root.parents)):raise ValueError('apply_unsafe_path')
    if not re.fullmatch(r'cohort-[a-f0-9]{20}',cycle_id):raise ValueError('apply_cycle_invalid')
    profile=learning_profile.require(root,policy,'apply')
    if profile.get('autoApply') is not True:raise ValueError('apply_authorization_required')
    grant=require_operation(root,policy,'quality_evaluate')
    with read_only_validation(),_db(root) as db:
        old=db.execute('SELECT status,evidence FROM cycles WHERE cycle_id=?',(cycle_id,)).fetchone()
        manifest=db.execute('SELECT manifest FROM learning_jobs WHERE job_id=?',(cycle_id,)).fetchone()
        train_rows,validation=training_and_validation(db,Path(profile['projectRoot']),grant)
    if old is None or old[0] not in ('staged_evaluated','applied') or manifest is None:raise ValueError('apply_quality_required')
    stage=json.loads(old[1]);job=json.loads(manifest[0]);train=LearningQueue(root).rows(job,train_rows)
    artifact=root/'cycles'/cycle_id/'candidate.artifact'
    if any(p.is_symlink() for p in artifact.parents):raise ValueError('apply_unsafe_path')
    if (stage['artifactPath']!=str(artifact) or _hash(artifact)!=stage['artifactSha256']
            or not stage['sourceActiveAtStage'] or stage['authorizationSha256']!=proposal_sha256(grant)
            or job['approval']!=proposal_sha256(grant) or profile['runtimeApprovalSha256']!=proposal_sha256(grant)
            or stage['model_input_view']!=profile['view']
            or stage['baseArtifactSha256']!=_hash(policy['baseArtifact'])
            or stage['trainEvidenceSha256']!=_case_evidence(train)
            or not _quality_ready(train,validation,grant['cohort'])[0]
            or any(not _case_active(root,(r[7],r[1],r[3])) for r in train+validation)):
        raise ValueError('apply_source_or_artifact_changed')
    metrics=stage['metrics'];before,after=metrics['incumbent'],metrics['candidate']
    evaluated=metrics.get('evaluatedAt')
    if (type(evaluated)not in(int,float) or not math.isfinite(evaluated)
            or not 0<=time.time()-evaluated<=profile['maxQualityAgeSeconds']
            or metrics.get('validationEvidenceSha256')!=_case_evidence(validation)
            or metrics.get('validation_questions')!=len(validation)
            or stage.get('validationCases')!=[{'projectRoot':r[7],'requestId':r[1],'inputSha256':r[2]} for r in validation]):
        raise ValueError('apply_quality_stale')
    for metric in ('exact_rate','necessary_recall','irrelevant_injection_rate','none_exact_rate'):
        if any(type(score.get(metric))not in(int,float) or not math.isfinite(score[metric]) or not 0<=score[metric]<=(3 if metric=='irrelevant_injection_rate' else 1) for score in (before,after)):
            raise ValueError('apply_quality_invalid')
        if (after[metric]>before[metric] if metric=='irrelevant_injection_rate' else after[metric]<before[metric]):
            raise ValueError('apply_quality_regression')
    if after['exact_rate']<policy['targetExact']:raise ValueError('apply_quality_below_target')
    current=read_private_json(root/'active-checkpoint.json')
    incumbent=expected['old_pointer'] if expected else current
    if metrics.get('incumbentArtifactSha256')!=incumbent['artifact_sha256']:
        raise ValueError('apply_incumbent_changed')
    if expected and (current not in (expected['old_pointer'],expected['new_pointer'])
            or digest(profile)!=expected['profile_sha256'] or digest(stage)!=expected['stage_sha256']):
        raise ValueError('apply_intent_changed')
    if expected:
        apply_id=digest([str(root),cycle_id,digest(profile),digest(stage),incumbent])[:32]
        pointer=_new_pointer(stage,profile,cycle_id,apply_id)
        stamp=expected['new_receipt'].get('createdAt')
        if (expected['namespace_root']!=str(root) or expected['cycle_id']!=cycle_id or expected['apply_id']!=apply_id
                or expected['new_pointer']!=pointer or expected['old_receipt']['checkpoint_sha256']!=digest(incumbent)
                or type(stamp)not in(int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+1
                or expected['new_receipt']!={**expected['old_receipt'],'checkpoint_sha256':digest(pointer),'createdAt':stamp}):
            raise ValueError('apply_journal_binding_changed')
    if _hash(incumbent['artifact_path'])!=incumbent['artifact_sha256']:raise ValueError('apply_incumbent_changed')
    return profile,stage,incumbent

def _new_pointer(stage,profile,cycle_id,apply_id):
    return {'schema_version':1,'artifact_path':stage['artifactPath'],'artifact_sha256':stage['artifactSha256'],
         'cycle_id':cycle_id,'model_input_view':profile['view'],'application':{'schema':SCHEMA,'id':apply_id}}

def _intent(root,policy,cycle_id):
    profile,stage,old=validate_candidate(root,policy,cycle_id)
    receipt=read_private_json(root/identity.RECEIPT)
    if (receipt['schema']!=identity.SCHEMA or receipt['view']!=profile['view']
            or receipt['verified']['base_model_sha256']!=profile['modelIdentitySha256']
            or receipt['checkpoint_sha256']!=digest(old)
            or read_private_json(root/identity.BINDING)!={'schema':identity.SCHEMA,'receipt_sha256':digest(receipt)}):
        raise ValueError('apply_prepared_inference_required')
    apply_id=digest([str(root),cycle_id,digest(profile),digest(stage),old])[:32]
    new=_new_pointer(stage,profile,cycle_id,apply_id)
    new_receipt={**deepcopy(receipt),'checkpoint_sha256':digest(new),'createdAt':time.time()}
    return {'schema':SCHEMA,'apply_id':apply_id,'cycle_id':cycle_id,'namespace_root':str(root),
        'profile_sha256':digest(profile),'stage_sha256':digest(stage),'old_pointer':old,'new_pointer':new,
        'old_receipt':receipt,'new_receipt':new_receipt,'phase':'intent','token':None,'attempts':0,'retry_at':0}

def _check_receipts(root,row):
    receipt=read_private_json(root/identity.RECEIPT)
    binding=read_private_json(root/identity.BINDING)
    if receipt not in (row['old_receipt'],row['new_receipt']) or binding not in (
            {'schema':identity.SCHEMA,'receipt_sha256':digest(row['old_receipt'])},
            {'schema':identity.SCHEMA,'receipt_sha256':digest(row['new_receipt'])}):raise ValueError('apply_inference_evidence_changed')

def _finish_cycle(root,cycle_id):
    from .shared import _db
    with _db(root) as db:db.execute("UPDATE cycles SET status='applied' WHERE cycle_id=? AND status='staged_evaluated'",(cycle_id,))

def apply_candidate(root,policy,cycle_id,*,transport=None,fault=lambda _:None):
    root=resident._private_state(Path(root));transport=transport or service_transport(root,policy)
    if any(p.is_symlink() for p in (root,root/'cycles',*root.parents)):
        return {'status':'apply_blocked','reason':'apply_unsafe_path','cycle_id':cycle_id}
    resident._private_state(root/'cycles')
    fd=_lock(root/'candidate-apply.lock');pointer_fd=None;row=None
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'apply_deferred','reason':'apply_busy','cycle_id':cycle_id}
        pointer_fd=_lock(root/'cycles/cycle.lock')
        try:fcntl.flock(pointer_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'apply_deferred','reason':'pointer_busy','cycle_id':cycle_id}
        path=root/JOURNAL
        row=read_journal(root) if path.exists() or path.is_symlink() else None
        if row and row['cycle_id']!=cycle_id:
            if row['phase'] not in ('activated','rejected'):raise ValueError('apply_other_transaction_pending')
            _archive(root,row);row=None
        if row is None:
            row=_intent(root,policy,cycle_id);put(path,row);fault('intent')
        validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
        if row['phase']=='rejected':return {'status':'apply_blocked','reason':'candidate_rejected','cycle_id':cycle_id}
        if row['phase']=='activated':
            readable(root);_finish_cycle(root,cycle_id)
            return {'status':'already_applied','cycle_id':cycle_id}
        if row.get('retry_at',0)>time.time():return {'status':'apply_deferred','next_retry':row['retry_at'],'cycle_id':cycle_id}
        row['attempts']=row.get('attempts',0)+1;row['retry_at']=0;put(path,row)
        current=read_private_json(root/'active-checkpoint.json')
        if current==row['old_pointer']:
            answer=transport('prepare',row);fault('service_prepared')
            if answer.get('status')!='prepared' or not isinstance(answer.get('token'),str):raise ValueError('apply_prepare_failed')
            row['token']=answer['token'];row['phase']='prepared';put(path,row);fault('prepared')
            validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
            if read_private_json(root/'active-checkpoint.json')!=row['old_pointer']:raise ValueError('apply_pointer_cas_conflict')
            put(root/'active-checkpoint.json',row['new_pointer']);fault('pointer_written')
        elif current!=row['new_pointer']:raise ValueError('apply_pointer_cas_conflict')
        # Interrupted cross-file commit: only these exact old/new bytes reconcile.
        validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
        put(root/identity.RECEIPT,row['new_receipt']);fault('receipt_written')
        put(root/identity.BINDING,{'schema':identity.SCHEMA,'receipt_sha256':digest(row['new_receipt'])});fault('binding_written')
        row['phase']='pointer_committed';put(path,row);fault('pointer_committed')
        # Reconstruct preparation if the worker restarted or its bounded slot expired.
        answer=transport('prepare',row)
        if answer.get('status')!='prepared' or (row['token'] is not None and answer.get('token')!=row['token']):raise ValueError('apply_generation_changed')
        row['token']=answer['token'];put(path,row)
        validate_candidate(root,policy,cycle_id,expected=row)
        answer=transport('activate',row);fault('service_activated')
        if answer.get('status')!='activated' or answer.get('token')!=row['token']:raise ValueError('apply_activation_failed')
        validate_candidate(root,policy,cycle_id,expected=row)
        row['phase']='activated';put(path,row);fault('activated')
        _finish_cycle(root,cycle_id)
        return {'status':'applied','cycle_id':cycle_id,'artifactSha256':row['new_pointer']['artifact_sha256']}
    except ResourceDeferred as exc:
        retry=time.time()+min(300,30*2**min(max(0,(row or {}).get('attempts',1)-1),4))
        if row is not None:row['retry_at']=retry;put(root/JOURNAL,row)
        return {'status':'apply_deferred','reason':str(exc)[:80],'cycle_id':cycle_id,'next_retry':retry}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        if (row is not None and row['cycle_id']==cycle_id and row['phase'] not in ('activated','rejected')
                and read_private_json(root/'active-checkpoint.json')==row['old_pointer']):
            row['phase']='rejected';put(root/JOURNAL,row)
        return {'status':'apply_blocked','reason':str(exc)[:80],'cycle_id':cycle_id}
    finally:
        if pointer_fd is not None:os.close(pointer_fd)
        os.close(fd)

def service_transport(root,policy):
    def exchange(command,row):
        import socket
        from . import inference_service as service
        deadline=time.monotonic()+30
        address=service.service_root(create=True)
        transient={'resource_deferred','replacement_deferred','busy','deadline_expired','queue_full','starting'}
        try:
            service._ensure(address,policy['trainerArgv'][0],deadline)
            envelope={'command':'apply_'+command,'schema':service.PROTOCOL,'signature':service._signature(),
                'state_dir':str(root),'namespace':service.namespace(root),'apply_id':row['apply_id'],
                'queued_at_epoch':time.time(),'deadline_epoch':time.time()+max(0,deadline-time.monotonic())}
            answer=service._exchange(address,envelope,deadline)
        except socket.timeout as exc:raise ResourceDeferred('apply_deadline_expired') from exc
        except service.ServiceFailure as exc:
            if exc.code in transient:raise ResourceDeferred('apply_'+exc.code) from exc
            raise
        if answer.get('status') in transient:
            raise ResourceDeferred('apply_'+answer['status'])
        return answer
    return exchange
