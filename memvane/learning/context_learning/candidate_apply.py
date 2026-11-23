"""Scoped local candidate publication; no training, teacher or DB rollback."""
from copy import deepcopy
from pathlib import Path
import fcntl,json,math,os,re,time
from .dataset import read_private_json
from .local_cycle import _safe_file,_hash,_atomic_json,ResourceDeferred
from .learning_profile import digest
from memvane.runtime.api import coordination_lock as _lock
from .training_cursor import _sync
from . import inference_identity as identity,resident

JOURNAL='candidate-apply.json'
# Application refusals that judge the candidate itself (``validate_candidate`` and a
# journal already ``rejected``). The candidate's evaluation is closed on them.
# ``apply_quality_below_target`` is no longer raised: it stays for journals an older bundle wrote.
JUDGED=('apply_quality_regression','apply_quality_not_improved','apply_quality_below_target',
        'apply_quality_invalid','candidate_rejected')
# The resident answered a prepare or activate request with a failure: it refused this
# candidate. Its momentary states (busy, starting, out of time) reach here as
# ``ResourceDeferred``, and an evaluation it found stale as ``STALE`` (nothing is decided). A request that got no usable answer (a closing resident, another
# bundle's resident during an update) is not a refusal: it is one of the other failures.
REFUSED=('apply_prepare_failed','apply_activation_failed')
# The resident failed while it checked or loaded the candidate (``worker_error``: an
# exception, not an answer about the candidate). One of the other failures: tried again,
# counted, and ended at the limit.
SERVICE_ERROR='apply_service_error'
# The status words a resident answers with (``inference_service._CODES``); the journal and
# the result carry the one received as ``serviceStatus``, any other word as ``other``.
SERVICE_STATUSES=('queue_full','deadline_expired','resource_deferred','replacement_deferred',
    'replacement_failed','load_failed','model_family_mismatch','request_changed','request_rejected',
    'inference_attestation_required','worker_error','version_mismatch','busy','starting',
    'permission_denied','resource_unavailable','invalid_prediction','input_budget_exceeded',
    'apply_quality_stale','prepared','activated')

def _unaccepted(row,answer,refusal):
    """Record what the resident answered instead of accepting, and raise the failure it is."""
    status=answer.get('status') if isinstance(answer,dict) else None
    row['serviceStatus']=status if status in SERVICE_STATUSES else 'other'
    raise ValueError(SERVICE_ERROR if status=='worker_error' else refusal)

# A failure whose text is not one of this module's own codes: an operating-system error's
# message, a parser's sentence. Rows and events carry these words, never the text.
IO_ERROR='apply_io_error'
OTHER_ERROR='apply_error'

def recorded_status(root,cycle_id):
    """The ``serviceStatus`` the live journal of ``cycle_id`` holds, or None; never raises."""
    try:
        row=read_journal(root)
        status=row.get('serviceStatus') if row.get('cycle_id')==cycle_id else None
        return status if status in SERVICE_STATUSES or status=='other' else None
    except (OSError,ValueError,KeyError,TypeError,AttributeError):return None
# The journal no longer describes this application (changed bytes, or what it was formed
# from changed after something was committed under it): no later attempt can use it.
UNUSABLE=('apply_journal_binding_changed','apply_intent_changed')
# The evaluation is too old or its cases changed: nothing is decided, it is evaluated again.
STALE='apply_quality_stale'
# The active artifact's bytes are not the pointer's: nothing a pass does changes that.
INCUMBENT_DAMAGED='apply_incumbent_artifact_changed'
# Any other failure with a journal is tried again; this many in a row end the application.
FAILURE_LIMIT=5

# What the resident's momentary states and start failures reach here as (``service_transport``).
TRANSIENT=('resource_deferred','replacement_deferred','busy','deadline_expired','queue_full','starting',
    'start_refused','start_path_invalid','start_io_failed','start_failed')
# Every reason a row or an event may carry: the codes of this module, the resident's status
# words, and the codes of the checks an application runs (the evidence, the wire, the profile).
# A text that is not listed here is never kept, whatever it looks like.
REASONS=frozenset((
    *JUDGED,*REFUSED,*UNUSABLE,STALE,INCUMBENT_DAMAGED,SERVICE_ERROR,IO_ERROR,OTHER_ERROR,*SERVICE_STATUSES,
    *('apply_'+word for word in (*TRANSIENT,'permission_denied')),
    'apply_activation_pending','apply_archive_changed','apply_authorization_required','apply_cycle_invalid',
    'apply_generation_changed','apply_incumbent_changed','apply_inference_evidence_changed',
    'apply_journal_invalid','apply_other_transaction_pending','apply_pointer_cas_conflict',
    'apply_pointer_invalid','apply_prepared_inference_required','apply_quality_required',
    'apply_source_or_artifact_changed','apply_unsafe_path','apply_busy','pointer_busy',
    'resident_wire_invalid'))

def _code(exc):
    """The fixed reason of a failure: one of ``REASONS``, or the word for its kind."""
    if isinstance(exc,OSError):return IO_ERROR
    text=exc.args[0] if isinstance(exc,(ValueError,ResourceDeferred)) and exc.args and isinstance(exc.args[0],str) else None
    return text if text in REASONS else OTHER_ERROR


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
    live=root/JOURNAL
    row=read_journal(root) if live.exists() or live.is_symlink() else None
    if row is None or row['apply_id']!=marker['id']:
        # No live journal: an owner's restore removed a later candidate's uncommitted one.
        try:row=read_private_json(root/ARCHIVE/(marker['id']+'.json'))
        except FileNotFoundError:raise ValueError('apply_activation_pending') from None
    # The receipt is the one bound now and it names this pointer. Its bytes need not be the
    # journal's: inference preparation writes a new receipt for the same pointer after a plugin
    # update, and ``inference_identity.admit`` is what judges the receipt itself.
    receipt=read_private_json(root/identity.RECEIPT)
    if (row.get('schema')!=SCHEMA or row.get('phase')!='activated' or row.get('new_pointer')!=pointer
            or not isinstance(receipt,dict) or receipt.get('checkpoint_sha256')!=digest(pointer)
            or read_private_json(root/identity.BINDING)!={'schema':identity.SCHEMA,'receipt_sha256':digest(receipt)}):
        raise ValueError('apply_activation_pending')

def validate_candidate(root,policy,cycle_id,*,expected=None):
    """Recheck real admission, actual trained batch, fresh disjoint quality."""
    from . import learning_profile
    from .runtime_authorization import require_operation,proposal_sha256
    from .learning_queue import LearningQueue
    from .automatic_cohort import training_and_validation
    from .automatic_staging import _case_evidence,_quality_ready
    from .prequential import min_nonempty_validation
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
    stage=json.loads(old[1]);job=json.loads(manifest[0])
    artifact=root/'cycles'/cycle_id/'candidate.artifact'
    if any(p.is_symlink() for p in artifact.parents):raise ValueError('apply_unsafe_path')
    current=read_private_json(root/'active-checkpoint.json')
    # Once the pointer is the candidate's the decision is made: only the identity of what
    # was decided is checked again (the artifact, and below the journal's stage, profile and
    # binding), so new gold, an older evaluation, an edited member card or a renewed approval
    # cannot strand an application between its pointer and its activation. A source revoked
    # in that span is handled as after any finished application: the pointer does not go back.
    decided=expected is not None and current==expected['new_pointer']
    # Before every other comparison (the base model can be the active artifact, and an
    # evaluation of damaged bytes records their hash). Not after the decision: the pointer
    # is the candidate's and the replaced artifact is no longer what is served, so its
    # bytes cannot stop the activation.
    if not decided and incumbent_damaged(expected['old_pointer'] if expected else current):raise ValueError(INCUMBENT_DAMAGED)
    if decided:
        if stage['artifactPath']!=str(artifact) or _hash(artifact)!=stage['artifactSha256']:
            raise ValueError('apply_source_or_artifact_changed')
        train=None
    else:train=LearningQueue(root).rows(job,train_rows)
    if not decided and (stage['artifactPath']!=str(artifact) or _hash(artifact)!=stage['artifactSha256']
            or not stage['sourceActiveAtStage'] or stage['authorizationSha256']!=proposal_sha256(grant)
            or job['approval']!=proposal_sha256(grant) or profile['runtimeApprovalSha256']!=proposal_sha256(grant)
            or stage['model_input_view']!=profile['view']
            or stage['baseArtifactSha256']!=_hash(policy['baseArtifact'])
            or stage['trainEvidenceSha256']!=_case_evidence(train)
            or not _quality_ready(train,validation,grant['cohort'],
                                  min_nonempty=min_nonempty_validation(grant))[0]
            or any(not _case_active(root,(r[7],r[1],r[3])) for r in train+validation)):
        raise ValueError('apply_source_or_artifact_changed')
    metrics=stage['metrics'];before,after=metrics['incumbent'],metrics['candidate']
    evaluated=metrics.get('evaluatedAt')
    if not decided and (type(evaluated)not in(int,float) or not math.isfinite(evaluated)
            or not 0<=time.time()-evaluated<=profile['maxQualityAgeSeconds']
            or metrics.get('validationEvidenceSha256')!=_case_evidence(validation)
            or metrics.get('validation_questions')!=len(validation)
            or stage.get('validationCases')!=[{'projectRoot':r[7],'requestId':r[1],'inputSha256':r[2]} for r in validation]):
        raise ValueError('apply_quality_stale')
    for metric in () if decided else ('exact_rate','necessary_recall','irrelevant_injection_rate','none_exact_rate'):
        if any(type(score.get(metric))not in(int,float) or not math.isfinite(score[metric]) or not 0<=score[metric]<=(3 if metric=='irrelevant_injection_rate' else 1) for score in (before,after)):
            raise ValueError('apply_quality_invalid')
        if (after[metric]>before[metric] if metric=='irrelevant_injection_rate' else after[metric]<before[metric]):
            raise ValueError('apply_quality_regression')
    # Applied only when exact improves; equal is not an improvement. The policy file's
    # ``targetExact`` stays in the file and is not read here.
    if not decided and not after['exact_rate']>before['exact_rate']:raise ValueError('apply_quality_not_improved')
    incumbent=expected['old_pointer'] if expected else current
    if metrics.get('incumbentArtifactSha256')!=incumbent['artifact_sha256']:
        raise ValueError('apply_incumbent_changed')
    if expected:
        # After the pointer a plugin update may have rewritten the profile's derived fields
        # (approval, code and policy hashes, trainer argv): what the owner chose is compared
        # without them. A journal of an earlier bundle has no such digest and compares whole.
        same=(_stable(profile)==expected['profile_stable_sha256'] if decided and 'profile_stable_sha256' in expected
              else digest(profile)==expected['profile_sha256'])
        if (current not in (expected['old_pointer'],expected['new_pointer']) or not same
                or digest(stage)!=expected['stage_sha256']):
            raise ValueError('apply_intent_changed')
    if expected and decided:
        if (expected['namespace_root']!=str(root) or expected['cycle_id']!=cycle_id
                or expected['old_receipt']['checkpoint_sha256']!=digest(incumbent)):
            raise ValueError('apply_journal_binding_changed')
    elif expected:
        apply_id=digest([str(root),cycle_id,digest(profile),digest(stage),incumbent])[:32]
        pointer=_new_pointer(stage,profile,cycle_id,apply_id)
        stamp=expected['new_receipt'].get('createdAt')
        if (expected['namespace_root']!=str(root) or expected['cycle_id']!=cycle_id or expected['apply_id']!=apply_id
                or expected['new_pointer']!=pointer or expected['old_receipt']['checkpoint_sha256']!=digest(incumbent)
                or type(stamp)not in(int,float) or not math.isfinite(stamp) or not 0<stamp<=time.time()+1
                or expected['new_receipt']!={**expected['old_receipt'],'checkpoint_sha256':digest(pointer),'createdAt':stamp}):
            raise ValueError('apply_journal_binding_changed')
    return profile,stage,incumbent

def incumbent_damaged(pointer):
    """Whether the active artifact's bytes are not the pointer's; a file that is gone or cannot be read safely is."""
    try:return _hash(pointer['artifact_path'])!=pointer['artifact_sha256']
    except (OSError,ValueError,KeyError,TypeError):return True

def _stable(profile):
    """The profile's digest without the fields a plugin update derives again."""
    from memvane.learning.default_renewal_plan import PROFILE_DERIVED
    return digest({key:value for key,value in profile.items() if key not in PROFILE_DERIVED})

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
        'profile_sha256':digest(profile),'profile_stable_sha256':_stable(profile),
        'stage_sha256':digest(stage),'old_pointer':old,'new_pointer':new,
        'old_receipt':receipt,'new_receipt':new_receipt,'phase':'intent','token':None,'attempts':0,'retry_at':0}

def _bound_to_new(root,row):
    """Whether the receipt bound now names the journal's new pointer (its own, or one prepared again since)."""
    receipt=read_private_json(root/identity.RECEIPT)
    return (isinstance(receipt,dict) and receipt.get('checkpoint_sha256')==digest(row['new_pointer'])
            and read_private_json(root/identity.BINDING)=={'schema':identity.SCHEMA,'receipt_sha256':digest(receipt)})

def _check_receipts(root,row):
    receipt=read_private_json(root/identity.RECEIPT)
    binding=read_private_json(root/identity.BINDING)
    # Under the candidate's pointer, evidence prepared again for that pointer (a plugin update) is the journal's too.
    if read_private_json(root/'active-checkpoint.json')==row['new_pointer'] and _bound_to_new(root,row):return
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
        # Nothing is committed while the pointer is the incumbent's: an ``intent``, and a
        # ``prepared`` whose pointer was never written (the resident's pending object expires).
        uncommitted=(row is not None and row['phase'] in ('intent','prepared')
                     and read_private_json(root/'active-checkpoint.json')==row['old_pointer'])
        carried=0
        if row and row['cycle_id']!=cycle_id:
            # Another candidate's uncommitted journal is replaced; a finished one is archived.
            if row['phase'] not in ('activated','rejected') and not uncommitted:raise ValueError('apply_other_transaction_pending')
            if not uncommitted:_archive(root,row)
            # The backoff of those failures holds for this candidate too.
            elif row.get('retry_at',0)>time.time():
                return {'status':'apply_deferred','next_retry':row['retry_at'],'cycle_id':cycle_id}
            # Its failures in a row are not this candidate's fault either (a resident that
            # errs, a socket that fails): the count goes on, or two candidates taking turns
            # would each start at zero for ever. A finished journal ends the run of failures.
            if uncommitted:carried=row.get('failures',0) if type(row.get('failures',0)) is int else 0
            row=None
        if row is not None and uncommitted and row.get('retry_at',0)>time.time():
            return {'status':'apply_deferred','next_retry':row['retry_at'],'cycle_id':cycle_id}
        if row is None or uncommitted:
            # It is formed again from what is true now, so a re-evaluation never leaves a
            # journal that can only fail. Its count of failures is kept.
            fresh=_intent(root,policy,cycle_id)
            fresh['failures']=row.get('failures',0) if row is not None else carried
            row=fresh;put(path,row);fault('intent')
        validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
        if row['phase']=='rejected':return {'status':'apply_blocked','reason':'candidate_rejected','cycle_id':cycle_id}
        if row['phase']=='activated':
            readable(root);_finish_cycle(root,cycle_id)
            return {'status':'already_applied','cycle_id':cycle_id}
        if row.get('retry_at',0)>time.time():return {'status':'apply_deferred','next_retry':row['retry_at'],'cycle_id':cycle_id}
        row['attempts']=row.get('attempts',0)+1;row['retry_at']=0;row.pop('serviceStatus',None);put(path,row)
        current=read_private_json(root/'active-checkpoint.json')
        if current==row['old_pointer']:
            answer=transport('prepare',row);fault('service_prepared')
            if answer.get('status')==STALE:raise ValueError(STALE)
            if answer.get('status')!='prepared' or not isinstance(answer.get('token'),str):_unaccepted(row,answer,'apply_prepare_failed')
            # ``failures`` goes back to zero only at ``pointer_committed``: a failure that recurs between
            # this point and the pointer would otherwise never reach the limit (each new intent keeps the count).
            row['token']=answer['token'];row['phase']='prepared';put(path,row);fault('prepared')
            validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
            if read_private_json(root/'active-checkpoint.json')!=row['old_pointer']:raise ValueError('apply_pointer_cas_conflict')
            put(root/'active-checkpoint.json',row['new_pointer']);fault('pointer_written')
        elif current!=row['new_pointer']:raise ValueError('apply_pointer_cas_conflict')
        # Interrupted cross-file commit: only these exact old/new bytes reconcile.
        validate_candidate(root,policy,cycle_id,expected=row);_check_receipts(root,row)
        # Evidence already bound to the new pointer is kept: a receipt prepared again is the current one.
        bound=_bound_to_new(root,row)
        if not bound:put(root/identity.RECEIPT,row['new_receipt'])
        fault('receipt_written')
        if not bound:put(root/identity.BINDING,{'schema':identity.SCHEMA,'receipt_sha256':digest(row['new_receipt'])})
        fault('binding_written')
        if row['phase']!='pointer_committed':row['failures']=0
        row['phase']='pointer_committed';put(path,row);fault('pointer_committed')
        # Reconstruct preparation if the worker restarted or its bounded slot expired.
        answer=transport('prepare',row)
        if answer.get('status')!='prepared' or (row['token'] is not None and answer.get('token')!=row['token']):
            # The answer is kept beside the reason: a resident that errs is told apart from one that moved on.
            status=answer.get('status') if isinstance(answer,dict) else None
            if status!='prepared':row['serviceStatus']=status if status in SERVICE_STATUSES else 'other'
            raise ValueError('apply_generation_changed')
        row['token']=answer['token'];put(path,row)
        validate_candidate(root,policy,cycle_id,expected=row)
        answer=transport('activate',row);fault('service_activated')
        if answer.get('status')!='activated' or answer.get('token')!=row['token']:_unaccepted(row,answer,'apply_activation_failed')
        validate_candidate(root,policy,cycle_id,expected=row)
        row['phase']='activated';put(path,row);fault('activated')
        _finish_cycle(root,cycle_id)
        return {'status':'applied','cycle_id':cycle_id,'artifactSha256':row['new_pointer']['artifact_sha256']}
    except ResourceDeferred as exc:
        retry=time.time()+min(300,30*2**min(max(0,(row or {}).get('attempts',1)-1),4))
        if row is not None:row['retry_at']=retry;put(root/JOURNAL,row)
        return {'status':'apply_deferred','reason':_code(exc),'cycle_id':cycle_id,'next_retry':retry}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        reason=_code(exc)
        if (reason!=STALE and row is not None and row['cycle_id']==cycle_id
                and row['phase'] not in ('activated','rejected')):
            active=read_private_json(root/'active-checkpoint.json')
            if active==row['new_pointer']:
                # The candidate's pointer is written: the application cannot be ended here.
                # Every pass tries it again; the journal says how often and why it failed.
                row['failures']=row.get('failures',0)+1;row['reason']=reason;put(root/JOURNAL,row)
                return {'status':'apply_blocked','reason':reason,'cycle_id':cycle_id,'failures':row['failures'],
                        'activationPending':row['failures']>=FAILURE_LIMIT,**_service(row)}
            if active==row['old_pointer']:
                if reason not in JUDGED+REFUSED+UNUSABLE:
                    row['failures']=row.get('failures',0)+1
                    if row['failures']<FAILURE_LIMIT:
                        retry=time.time()+min(300,30*2**min(row['failures']-1,4))
                        row['retry_at']=retry;put(root/JOURNAL,row)
                        return {'status':'apply_deferred','reason':reason,'cycle_id':cycle_id,
                                'next_retry':retry,'failures':row['failures'],**_service(row)}
                # A judgment, the resident's refusal, an unusable journal, or the limit of other failures.
                row['phase']='rejected';row['reason']=reason;put(root/JOURNAL,row)
            # A pointer that is neither of the journal's is someone else's change: left as it is.
        return {'status':'apply_blocked','reason':reason,'cycle_id':cycle_id,**_service(row)}
    finally:
        if pointer_fd is not None:os.close(pointer_fd)
        os.close(fd)

def _service(row):
    """``{'serviceStatus': ...}`` when the failure was the resident's answer."""
    status=(row or {}).get('serviceStatus')
    return {'serviceStatus':status} if isinstance(status,str) else {}

def service_transport(root,policy):
    def exchange(command,row):
        import socket
        from . import inference_service as service
        deadline=time.monotonic()+30
        address=service.service_root(create=True)
        transient=set(TRANSIENT)
        try:
            try:service._ensure(address,policy['trainerArgv'][0],deadline)
            except service.ServiceFailure as exc:
                # A local startup permission error does not reject a trained candidate.
                # A permission_denied reply from the running resident still does.
                if exc.code=='permission_denied':raise ResourceDeferred('apply_'+exc.code) from exc
                raise
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
