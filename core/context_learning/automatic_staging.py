"""Normal-worker local training with no trial or active pointer publication.

This is invoked by shared.train_due under the same worker lock and resource
monitor as the existing scheduler. It uses only v2 authorization cohort rows.
An insufficient validation cohort leaves a hash-pinned trained artifact staged.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import job_telemetry

from .automatic_cohort import training_and_validation
from .contracts import digest
from .local_cycle import VerifiedLayaTrainer, _hash, ResourceDeferred, record_resource_defer
from .runtime_authorization import proposal_sha256, require_operation
from .store import canonical


def _case_evidence(rows):
    return digest(canonical([(r[0], r[1], r[2], r[5]) for r in rows]))


def _project(root, grant):
    from .project_scope import SCOPE
    if grant.get('scope') == SCOPE:
        return Path(grant['projectRoot'])
    from .synthetic_experiment import project_for
    return project_for(root)


def _trainer(policy, factory, *, root=None, control=None, fds=(), coordination_fd=None):
    deadline=None
    if root is not None:
        import project_learning_setup
        if project_learning_setup._synthetic_marker(root,policy):
            setup=project_learning_setup.read_private_json(
                project_learning_setup._path(Path(root)/project_learning_setup.NAME,private=True))
            project=Path(setup['plan']['projectRoot'])
            budget=project_learning_setup.synthetic_budget_status(root,project,policy)
            if budget['status']!='ready':
                raise ResourceDeferred('synthetic_training_budget_exhausted',{'reason':budget['status']})
            receipt=project_learning_setup.read_private_json(
                project_learning_setup._path(Path(root)/project_learning_setup.BUDGET,private=True))
            remaining=(receipt['deadlineUnixMs']-time.time()*1000)/1000-2
            if remaining<1:
                raise ResourceDeferred('synthetic_training_budget_exhausted',{'reason':'time_exhausted'})
            deadline=time.monotonic()+remaining
    if factory is not None:
        trainer=factory(policy)
        trainer.learning_control=control;trainer.inherited_fds=fds;trainer.coordination_fd=coordination_fd
        return trainer
    adapter = Path(__file__).with_name('laya_adapter.py').resolve()
    if Path(policy['trainerArgv'][1]).resolve() != adapter:
        raise ValueError('invalid_shared_trainer')
    from .shared import resource_report
    trainer = VerifiedLayaTrainer(policy['trainerArgv'], proof_cache_dir=root,
        deadline=deadline,
        max_seconds=policy['safetySeconds'], max_rss_mib=policy['maxRssMib'],
        pressure_probe=lambda **options: resource_report(policy, detailed=True, admission=False,**options), require_monitor=True)
    # Automatic training cannot silently renew a compatibility smoke receipt.
    trainer.validate_prediction(cwd=root)
    trainer.learning_control=control;trainer.inherited_fds=fds;trainer.coordination_fd=coordination_fd
    return trainer


def _quality_ready(train, validation, cohort):
    from .project_scope import COHORT_SCHEMA
    if cohort.get('schema') == COHORT_SCHEMA:
        train_families = len({row[12] for row in train})
        validation_families = len({row[12] for row in validation})
    else:
        train_families = sum(any(row[12].startswith(prefix) for row in train)
                         for prefix in cohort['trainEditPrefixes'])
        validation_families = sum(any(row[12].startswith(prefix)
                                  for row in validation)
                              for prefix in cohort['validationEditPrefixes'])
    empty = sum(not json.loads(row[5])['selected_ids'] for row in validation)
    ready = (len(train) >= cohort['minQualityTrainCases']
             and train_families >= cohort['minTrainFamilies']
             and len(validation) >= cohort['minValidationCases']
             and validation_families >= cohort['minValidationFamilies']
             and empty >= cohort['minEmptyValidation'])
    return ready, empty, train_families, validation_families


def train_due_staged(root, policy, grant, *, trainer_factory=None, coordinator=None):
    from runtime_admission import preparation
    from .shared import resource_report
    try:
        with preparation():prepared=_prepare_staged(root,policy,grant)
    except ResourceDeferred as exc:
        record_resource_defer(Path(root),exc.report,reason=str(exc))
        return {'status':'deferred_resource','reason':str(exc)}
    if isinstance(prepared,dict):return prepared
    root,queue,job,train,validation,cohort_train=prepared
    if job['status']=='staged':
        return evaluate_pending(root,policy,job['id'],trainer_factory=trainer_factory,coordinator=coordinator)
    report=resource_report(policy,detailed=True)
    if not report['ready']:
        queue.defer(job['id'],'resource')
        record_resource_defer(root,report)
        return {'status':'deferred_resource','resources':report,'cycle_id':job['id'],'next_retry':queue.get(job['id'])['retry_at']}
    with queue.claim(job['id']) as claim:
        if claim is None:return {'status':'deferred_resource','cycle_id':job['id'],'next_retry':queue.get(job['id'])['retry_at']}
        from .resource_coordination import Coordinator
        coordinator=coordinator or Coordinator()
        try:
            with coordinator.training(policy,report) as lease:
                return _train_pinned(root,policy,grant,job,queue,train,validation,cohort_train,
                    trainer_factory=trainer_factory,claim_fd=claim,coordination=(coordinator,lease))
        except ResourceDeferred as exc:
            queue.defer(job['id'],'inference' if 'inference' in str(exc) else 'resource')
            record_resource_defer(root,exc.report,reason=str(exc))
            return {'status':'deferred_resource','cycle_id':job['id'],'next_retry':queue.get(job['id'])['retry_at']}



def _prepare_staged(root,policy,grant):
    from .shared import _db, _case_active
    from .synthetic_experiment import project_for
    from .laya_adapter import EDIT_INPUT_VIEW, model_identity
    from .learning_queue import LearningQueue
    root = Path(root)
    import project_learning_setup
    synthetic=project_learning_setup.synthetic_local_only(root,_project(root,grant),policy)
    if synthetic:
        budget=project_learning_setup.synthetic_budget_status(root,_project(root,grant),policy)
        if budget['status']!='ready':return budget
    if policy.get('modelInputView') != EDIT_INPUT_VIEW:
        return {'status':'multilingual_policy_required'}
    if (root/'experimental-checkpoint.json').exists():
        return {'status':'existing_trial_requires_review'}
    with _db(root) as db:
        cohort_train, validation = training_and_validation(db, _project(root, grant), grant)
    if len(cohort_train)>2000 or len(validation)>2000:
        return {'status':'cohort_source_revoked'}
    from runtime_admission import preparation_checkpoint
    def active(rows):
        result=[]
        for r in rows:
            preparation_checkpoint()
            if _case_active(root,(r[7],r[1],r[3])):result.append(r)
        return result
    cohort_train=active(cohort_train);validation=active(validation)
    if synthetic:
        empty=sum(not json.loads(row[5])['selected_ids'] for row in validation)
        candidate_count=sum(len(json.loads(row[3])['candidates']) for row in cohort_train)
        if len(validation)>50 or empty>10 or candidate_count>100:
            return {'status':'synthetic_training_budget_exhausted'}
    if len(cohort_train)<grant['cohort']['minTrainCases']:
        return {'status':'waiting_fresh_train','train':len(cohort_train),'required':grant['cohort']['minTrainCases']}
    family=digest(canonical([policy['trainerArgv'][0],model_identity(policy['trainerArgv'][2]),
                            _hash(policy['baseArtifact']),EDIT_INPUT_VIEW]))
    from . import learning_profile
    minimum=policy['batchMin']
    if learning_profile.present(root):
        profile=learning_profile.require(root,policy,'local_train')
        if not profile['localTrain']:return {'status':'disabled'}
        minimum=profile['minFreshTrainCases']
    queue=LearningQueue(root)
    if synthetic:
        with queue.connect() as db:
            queue.schema(db)
            ids=[row[0] for row in db.execute(
                'SELECT job_id FROM learning_jobs WHERE namespace=? ORDER BY rowid',(queue.scope,))]
        if len(ids)>1:return {'status':'synthetic_training_budget_exhausted'}
        existing=queue.get(ids[0]) if ids else None
        if existing and (existing['status']=='blocked' or existing['attempts']>=5):
            return {'status':'synthetic_training_budget_exhausted'}
    from .project_scope import SCOPE
    job=(existing if synthetic and existing else queue.plan(
        cohort_train,approval=proposal_sha256(grant),cohort=grant['cohort']['id'],
        family=family,minimum=minimum,maximum=policy['batchMax'],
        roles=['train']*len(cohort_train),cumulative=grant.get('scope')==SCOPE))
    if job is None:return {'status':'waiting_fresh_batch','threshold':minimum}
    if synthetic and (len(job['members'])>100 or job['approval']!=proposal_sha256(grant)):
        return {'status':'synthetic_training_budget_exhausted'}
    with _db(root) as db:
        previous=db.execute('SELECT status FROM cycles WHERE cycle_id=?',(job['id'],)).fetchone()
    if previous and previous[0]=='applied':return {'status':'already_applied','cycle_id':job['id']}
    try: train=queue.rows(job,cohort_train)
    except ValueError:
        return {'status':'staged_source_revoked','cycle_id':job['id']}
    if job['status']=='blocked':
        return {'status':'staged_source_revoked' if job['reason']=='source_revoked' else 'learning_job_blocked','cycle_id':job['id']}
    if synthetic and sum(len(json.loads(row[3])['candidates']) for row in train)>100:
        return {'status':'synthetic_training_budget_exhausted'}
    return root,queue,job,train,validation,cohort_train


def _train_pinned(root,policy,grant,job,queue,train,validation,cohort_train,*,trainer_factory=None,claim_fd=None,coordination=None):
    from .shared import _case, _case_active, _db, _predict
    from .synthetic_experiment import project_for,reserve_train
    from .laya_adapter import EDIT_INPUT_VIEW
    from .resource_coordination import admission_policy
    controller,(coord_fd,admission)=coordination
    control={'schema':1,'root':str(controller.root),'policy':admission_policy(policy),
             'namespace':queue.scope,'job_id':job['id']}
    fds=tuple(fd for fd in (claim_fd,coord_fd) if fd is not None)
    grant_sha=proposal_sha256(grant)
    project=_project(root, grant)
    new_train=sum(m['fresh'] for m in job['members'])
    train_sha = _case_evidence(train)
    cycle_id = job['id']
    with _db(root) as db:
        old = db.execute('SELECT status,evidence FROM cycles WHERE cycle_id=?',
                         (cycle_id,)).fetchone()
        if old is None:
            if new_train < policy['batchMin']:
                return {'status': 'waiting_fresh_batch', 'new_train': new_train,
                        'threshold': policy['batchMin']}
            try:
                require_operation(root, policy, 'local_train',
                                  approval_sha256=grant_sha)
                from .project_scope import SCOPE
                if grant.get('scope') != SCOPE:
                    reserve_train(db, root, project, cycle_id)
            except ValueError as exc:
                return {'status': 'experiment_paused', 'reason': str(exc)[:80]}
            db.execute('INSERT INTO cycles VALUES (?,?,?,?,NULL)',
                (cycle_id, 'training', canonical({
                    'schema_version': 1, 'cohort_id': grant['cohort']['id'],
                    'authorizationSha256': grant_sha,
                    'trainEvidenceSha256': train_sha,
                    'model_input_view': EDIT_INPUT_VIEW}), time.time()))
        elif old[0] not in ('training', 'failed', 'deferred', 'staged', 'staged_evaluated'):
            return {'status': 'cohort_cycle_conflict', 'cycle_id': cycle_id}
    cycles = root / 'cycles'
    cycles.mkdir(mode=0o700, exist_ok=True)
    folder = cycles / cycle_id
    folder.mkdir(mode=0o700, exist_ok=True)
    artifact = folder / 'candidate.artifact'
    incumbent = Path(policy['baseArtifact'])
    base_sha = _hash(incumbent)
    if old and old[0] in ('staged', 'staged_evaluated'):
        stage = json.loads(old[1])
        if (stage.get('trainEvidenceSha256') != train_sha
                or stage.get('authorizationSha256') != grant_sha
                or stage.get('baseArtifactSha256') != base_sha
                or stage.get('artifactSha256') != _hash(artifact)):
            return {'status': 'staged_artifact_changed', 'cycle_id': cycle_id}
        if stage.get('sourceActiveAtStage') is not True:
            return {'status': 'staged_source_revoked', 'cycle_id': cycle_id}
        if old[0] == 'staged_evaluated':
            return {'status': 'already_staged_evaluated', 'cycle_id': cycle_id,
                    'metrics': stage['metrics']}
    else:
        meta = None
        start = None
        try:
            require_operation(root, policy, 'local_train',
                              approval_sha256=grant_sha)
            trainer = _trainer(policy, trainer_factory,root=root,control=control,fds=fds,coordination_fd=coord_fd)
            cases = [_case(row[:6]) for row in train]
            request = {'schema_version': 1,
                'train': [{k: v for k, v in case.items() if k != 'project_key'}
                          for case in cases],
                'artifact_path': str(artifact), 'manifest_sha256': train_sha,
                'model_input_view': EDIT_INPUT_VIEW,'learning_control':control}
            meta = {'job_id': cycle_id + ':train', 'parent_id': cycle_id,
                    'op': 'laya_train', 'runtime_kind': 'local_trainer',
                    'model_version': 'laya', 'source_version': 'base',
                    'destination_version': cycle_id}
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'queued', **meta)
            start = time.monotonic()
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'started',
                               queue_wait_ms=0, **meta)
            answer, train_resources = _train_once(trainer, request, folder)
            if answer != {'schema_version': 1, 'status': 'trained'}:
                raise ValueError('invalid_shared_train_result')
            source_active = all(_case_active(root, (r[7], r[1], r[3]))
                                for r in train)
            stage = {'schema_version': 1, 'cohort_id': grant['cohort']['id'],
                'authorizationSha256': grant_sha,
                'trainEvidenceSha256': train_sha,
                'artifactPath': str(artifact), 'artifactSha256': _hash(artifact),
                'baseArtifactSha256': base_sha,
                'model_input_view': EDIT_INPUT_VIEW,
                'trainCases': [{'projectRoot': row[7], 'requestId': row[1],
                                'inputSha256': row[2]} for row in train],
                'trainResources': train_resources,
                'sourceActiveAtStage': source_active,
                'publication': 'forbidden_without_separate_apply_authorization'}
            with _db(root) as db:
                latest, _ = training_and_validation(db, project, grant)
                if _case_evidence(queue.rows(job, latest)) != train_sha:
                    stage['sourceActiveAtStage'] = False
                db.execute("UPDATE cycles SET status='staged',evidence=? "
                           "WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                           (canonical(stage), cycle_id))
                if stage['sourceActiveAtStage']:
                    for row in train:
                        db.execute("UPDATE cases SET trained_cycle=? WHERE "
                            "project_key=? AND request_id=? AND input_sha256=? "
                            "AND status='gold' AND use_state='active' "
                            "AND trained_cycle IS NULL",
                            (cycle_id, row[0], row[1], row[2]))
                    queue.complete(cycle_id, db=db)
                else:
                    db.execute("UPDATE learning_jobs SET status='blocked',reason='source_revoked' WHERE job_id=?",(cycle_id,))
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'finished',
                status='success', run_ms=(time.monotonic() - start) * 1000,
                resources=train_resources, **meta)
            if not stage['sourceActiveAtStage']:
                return {'status': 'staged_source_revoked',
                        'cycle_id': cycle_id}
        except Exception as exc:
            deferred = isinstance(exc, ResourceDeferred)
            if deferred:
                record_resource_defer(root, exc.report, reason=str(exc))
            if meta is not None and start is not None:
                job_telemetry.emit(root / 'job-telemetry.jsonl', 'finished',
                    status='failure', run_ms=(time.monotonic() - start) * 1000,
                    error_type=type(exc).__name__, **meta)
            with _db(root) as db:
                db.execute("UPDATE cycles SET status=?,evidence=? "
                           "WHERE cycle_id=? AND status IN ('training','failed','deferred')",
                    ('deferred' if deferred else 'failed', canonical({'reason': str(exc)[:80],
                                'authorizationSha256': grant_sha,
                                'trainEvidenceSha256': train_sha}), cycle_id))
            queue.defer(cycle_id,'resource' if deferred else 'trainer_failed',failure=not deferred)
            return {'status': 'deferred_resource' if deferred else 'failed', 'reason': str(exc)[:80],
                    'cycle_id': cycle_id}
    return _evaluate_staged(root,policy,grant,job,stage,train,validation,cohort_train,
        trainer_factory=trainer_factory,coordination=coordination)


def _evaluate_staged(root,policy,grant,job,stage,train,validation,cohort_train,*,trainer_factory,coordination):
    from .evaluation_queue import EvaluationQueue
    from .resource_coordination import admission_policy
    from .synthetic_experiment import project_for
    from .shared import _db,_predict,_case,_case_active
    from .laya_adapter import EDIT_INPUT_VIEW
    cycle_id=job['id'];project=_project(root, grant);grant_sha=proposal_sha256(grant)
    try:require_operation(root,policy,'quality_evaluate',approval_sha256=grant_sha)
    except ResourceDeferred as exc:return {'status':'staged_evaluation_deferred','cycle_id':cycle_id,'reason':str(exc)}
    except ValueError:return {'status':'staged_authorization_expired','cycle_id':cycle_id}
    queue=EvaluationQueue(root)
    pin=digest(canonical([stage[k] for k in ('trainEvidenceSha256','artifactSha256','authorizationSha256','baseArtifactSha256','trainCases')]))
    evaluation=queue.ensure(cycle_id,pin,_case_evidence(validation))
    controller,(coord_fd,_)=coordination
    control={'schema':1,'root':str(controller.root),'policy':admission_policy(policy),'namespace':job['namespace'],'job_id':evaluation['id']}
    folder=root/'cycles'/cycle_id;incumbent=Path(policy['baseArtifact'])
    with queue.claim(evaluation['id']) as claim:
        if claim is None:
            if evaluation['status']=='waiting_quality':
                _,empty,tf,vf=_quality_ready(train,validation,grant['cohort'])
                return {'status':'staged_waiting_quality_cohort','cycle_id':cycle_id,
                    'train':len(train),'validation':len(validation),'train_families':tf,
                    'validation_families':vf,'empty_validation':empty,
                    'required_train':grant['cohort']['minQualityTrainCases'],
                    'required_validation':grant['cohort']['minValidationCases']}
            return {'status':'staged_evaluation_deferred','cycle_id':cycle_id}
        result=_evaluate_impl(root,policy,grant,stage,train,validation,cohort_train,
            cycle_id=cycle_id,project=project,grant_sha=grant_sha,folder=folder,
            incumbent=incumbent,trainer_factory=trainer_factory,control=control,
            fds=(claim,coord_fd),coord_fd=coord_fd,
            quality_epoch=digest(canonical([evaluation['id'],queue.get(evaluation['id'])['attempts']])))
        status=result['status']
        if status=='staged_evaluated':queue.finish(evaluation['id'],'completed')
        elif status=='staged_waiting_quality_cohort':queue.finish(evaluation['id'],'waiting_quality')
        elif status=='staged_evaluation_deferred':queue.defer(evaluation['id'],result.get('reason','resource'),failure=result.get('failure',False))
        else:queue.finish(evaluation['id'],'blocked')
        return result


def evaluate_pending(root,policy,cycle_id,*,trainer_factory=None,coordinator=None):
    """Quality-only recovery: never reserves or calls a training operation."""
    from runtime_admission import preparation
    from .shared import resource_report
    from .resource_coordination import Coordinator
    try:
        with preparation():prepared=_prepare_evaluation(root,policy,cycle_id)
    except ResourceDeferred as exc:
        return {'status':'staged_evaluation_deferred','cycle_id':cycle_id,'reason':str(exc)}
    if isinstance(prepared,dict):return prepared
    root,grant,queue,job,stage,train,validation,cohort_train,evaluations,evaluation=prepared
    controller=coordinator or Coordinator()
    try:
        with controller.training(policy,resource_report(policy,detailed=True)) as lease:
            return _evaluate_staged(root,policy,grant,job,stage,train,validation,cohort_train,
                trainer_factory=trainer_factory,coordination=(controller,lease))
    except ResourceDeferred as exc:
        evaluations.defer(evaluation['id'],'resource')
        return {'status':'staged_evaluation_deferred','cycle_id':cycle_id,'reason':str(exc)}



def _prepare_evaluation(root,policy,cycle_id):
    from .shared import _db,_case_active
    from .learning_queue import LearningQueue
    from .evaluation_queue import EvaluationQueue
    from .synthetic_experiment import project_for
    from .resource_coordination import Coordinator
    root=Path(root)
    grant=require_operation(root,policy,'quality_evaluate')
    queue=LearningQueue(root);job=queue.get(cycle_id)
    if job is None or job['approval']!=proposal_sha256(grant):return {'status':'staged_authorization_expired','cycle_id':cycle_id}
    with _db(root) as db:
        old=db.execute('SELECT status,evidence FROM cycles WHERE cycle_id=?',(cycle_id,)).fetchone()
        cohort_train,validation=training_and_validation(db,_project(root, grant),grant)
    if old is None or old[0] not in ('staged','staged_evaluated'):return {'status':'evaluation_stage_unavailable','cycle_id':cycle_id}
    stage=json.loads(old[1]);train=queue.rows(job,cohort_train)
    if (not stage['sourceActiveAtStage'] or stage['artifactSha256']!=_hash(stage['artifactPath'])
            or stage['baseArtifactSha256']!=_hash(policy['baseArtifact'])
            or any(not _case_active(root,(r[7],r[1],r[3])) for r in train+validation)):
        return {'status':'staged_source_revoked','cycle_id':cycle_id}
    if old[0]=='staged_evaluated':
        from .learning_profile import present,require
        refresh=False
        if present(root):
            from .dataset import read_private_json
            profile=require(root,policy,'quality_evaluate');metrics=stage['metrics']
            active=read_private_json(root/'active-checkpoint.json')
            refresh=(not 0<=time.time()-metrics.get('evaluatedAt',0)<=profile['maxQualityAgeSeconds']
                or metrics.get('validationEvidenceSha256')!=_case_evidence(validation)
                or metrics.get('incumbentArtifactSha256')!=active['artifact_sha256'])
        if not refresh:return {'status':'already_staged_evaluated','cycle_id':cycle_id,'metrics':stage['metrics']}
        with _db(root) as db:
            if db.execute("UPDATE cycles SET status='staged' WHERE cycle_id=? AND status='staged_evaluated' AND evidence=?",(cycle_id,old[1])).rowcount!=1:
                return {'status':'staged_cohort_changed','cycle_id':cycle_id}
        EvaluationQueue(root).refresh(cycle_id)
    evaluations=EvaluationQueue(root)
    pin=digest(canonical([stage[k] for k in ('trainEvidenceSha256','artifactSha256','authorizationSha256','baseArtifactSha256','trainCases')]))
    evaluation=evaluations.ensure(cycle_id,pin,_case_evidence(validation))
    return root,grant,queue,job,stage,train,validation,cohort_train,evaluations,evaluation


def _evaluate_impl(root,policy,grant,stage,train,validation,cohort_train,*,cycle_id,project,grant_sha,folder,incumbent,trainer_factory,control,fds,coord_fd,quality_epoch):
    from .shared import _db,_predict,_case,_case_active
    from .laya_adapter import EDIT_INPUT_VIEW
    artifact=Path(stage['artifactPath'])
    from .learning_profile import present,require
    active_anchor=None
    if present(root):
        require(root,policy,'quality_evaluate')
        from .dataset import read_private_json
        from .candidate_apply import readable
        readable(root);active_anchor=read_private_json(root/'active-checkpoint.json')
        incumbent=Path(active_anchor['artifact_path'])
    try:
        require_operation(root, policy, 'quality_evaluate',
                          approval_sha256=grant_sha)
    except ResourceDeferred as exc:
        return {'status':'staged_evaluation_deferred','cycle_id':cycle_id,'reason':str(exc)}
    except ValueError:
        return {'status': 'staged_authorization_expired', 'cycle_id': cycle_id}
    ready, empty, train_families, validation_families = _quality_ready(
        train, validation, grant['cohort'])
    if not ready:
        return {'status': 'staged_waiting_quality_cohort', 'cycle_id': cycle_id,
            'train': len(train), 'validation': len(validation),
            'train_families': train_families,
            'validation_families': validation_families,
            'empty_validation': empty,
            'required_train': grant['cohort']['minQualityTrainCases'],
            'required_train_families': grant['cohort']['minTrainFamilies'],
            'required_validation': grant['cohort']['minValidationCases'],
            'required_validation_families':
                grant['cohort']['minValidationFamilies'],
            'required_empty': grant['cohort']['minEmptyValidation'],
            'artifact_sha256': stage['artifactSha256']}
    if any(not _case_active(root, (r[7], r[1], r[3]))
           for r in train + validation):
        return {'status': 'staged_source_revoked', 'cycle_id': cycle_id}
    try:
        trainer = _trainer(policy, trainer_factory,root=root,control=control,fds=fds,coordination_fd=coord_fd)
        cases = [_case(row[:6]) for row in validation]
        before, before_resources = _predict(trainer, folder, incumbent, cases,
                                            'staged-incumbent',
                                            input_view=EDIT_INPUT_VIEW,cache_epoch=quality_epoch)
        after, after_resources = _predict(trainer, folder, artifact, cases,
                                          'staged-candidate',
                                          input_view=EDIT_INPUT_VIEW,cache_epoch=quality_epoch)
        require_operation(root, policy, 'quality_evaluate',
                          approval_sha256=grant_sha)
        metrics = {'incumbent': before, 'candidate': after,
                   'validation_questions': len(validation),
                   'empty_validation': empty,
                   'evaluation_scope': 'fresh_candidate_disjoint_edit_cohort',
                   'applied': False, 'evaluatedAt':time.time(),
                   'validationEvidenceSha256':_case_evidence(validation),
                   'incumbentArtifactSha256':_hash(incumbent),
                   'resources': {'incumbent': before_resources,
                                 'candidate': after_resources}}
        with _db(root) as db:
            if active_anchor is not None and read_private_json(root/'active-checkpoint.json')!=active_anchor:
                return {'status':'staged_cohort_changed','cycle_id':cycle_id}
            latest_train, latest_validation = training_and_validation(
                db, project, grant)
            if (_case_evidence(latest_train) != _case_evidence(cohort_train)
                    or _case_evidence(latest_validation) !=
                       _case_evidence(validation)):
                return {'status': 'staged_cohort_changed', 'cycle_id': cycle_id}
            if any(not _case_active(root, (r[7], r[1], r[3]))
                   for r in train + validation):
                return {'status': 'staged_source_revoked',
                        'cycle_id': cycle_id}
            if (root / 'experimental-checkpoint.json').exists():
                return {'status': 'existing_trial_requires_review'}
            db.execute("UPDATE cycles SET status='staged_evaluated',evidence=? "
                       "WHERE cycle_id=? AND status='staged'",
                       (canonical({**stage, 'validationCases': [
                           {'projectRoot': row[7], 'requestId': row[1],
                            'inputSha256': row[2]} for row in validation],
                           'metrics': metrics}), cycle_id))
        return {'status': 'staged_evaluated', 'cycle_id': cycle_id,
                'metrics': metrics}
    except Exception as exc:
        return {'status': 'staged_evaluation_deferred', 'cycle_id': cycle_id,
                'reason': str(exc)[:80],'failure':not isinstance(exc,ResourceDeferred)}


def _train_once(trainer,request,folder):
    """A durable completion is reused; uncertain final artifacts never retrain."""
    from .dataset import read_private_json
    from .local_cycle import _atomic_json
    from .training_cursor import request_identity, completion,recover_completed
    key=request_identity(request)
    intent={'schema':1,'request_sha256':key}
    path=folder/'train-intent.json'
    if path.exists() and read_private_json(path)!=intent:
        raise ValueError('learning_train_intent_changed')
    _atomic_json(path,intent)
    done=completion(request)
    if done is not None:return {'schema_version':1,'status':'trained'},{'recovered_completion':True}
    from runtime_admission import preparation
    cursor_path=folder/'resume/cursor.json'
    if cursor_path.exists() or cursor_path.is_symlink():
        with preparation():
            if recover_completed(request):return {'schema_version':1,'status':'trained'},{'recovered_cursor':True}
    if Path(request['artifact_path']).exists() or Path(request['artifact_path']).is_symlink():raise ValueError('learning_completion_uncertain')
    answer,resources=trainer.call('train',request,folder/'train.json',cwd=folder)
    if answer!={'schema_version':1,'status':'trained'}:raise ValueError('invalid_shared_train_result')
    from .training_cursor import record_completion
    record_completion(request)
    return answer,resources
