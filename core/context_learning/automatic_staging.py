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
from .local_cycle import VerifiedLayaTrainer, _hash
from .runtime_authorization import proposal_sha256, require_operation
from .store import canonical


def _case_evidence(rows):
    return digest(canonical([(r[0], r[1], r[2], r[5]) for r in rows]))


def _trainer(policy, factory):
    if factory is not None:
        return factory(policy)
    adapter = Path(__file__).with_name('laya_adapter.py').resolve()
    if Path(policy['trainerArgv'][1]).resolve() != adapter:
        raise ValueError('invalid_shared_trainer')
    from .shared import resource_report
    return VerifiedLayaTrainer(policy['trainerArgv'],
        max_seconds=policy['safetySeconds'], max_rss_mib=policy['maxRssMib'],
        pressure_probe=lambda: resource_report(policy), require_monitor=True)


def _quality_ready(train, validation, cohort):
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


def train_due_staged(root, policy, grant, *, trainer_factory=None):
    from .shared import (_case, _case_active, _db, resource_report,
                         _predict)
    from .synthetic_experiment import project_for, reserve_train
    from .laya_adapter import EDIT_INPUT_VIEW, model_identity
    root = Path(root)
    grant_sha = proposal_sha256(grant)
    if policy.get('modelInputView') != EDIT_INPUT_VIEW:
        return {'status': 'multilingual_policy_required'}
    if (root / 'experimental-checkpoint.json').exists():
        return {'status': 'existing_trial_requires_review'}
    resources = resource_report(policy, detailed=True)
    if not resources['ready']:
        return {'status': 'deferred_resource', 'resources': resources}
    project = project_for(root)
    with _db(root) as db:
        train, validation = training_and_validation(db, project, grant)
    if len(train) > 2000 or len(validation) > 2000:
        return {'status': 'cohort_source_revoked'}
    # A normal edit can retire one old card while other fresh cases remain
    # usable. Keep the retired cases in the ledger, but exclude them here.
    train = [r for r in train if _case_active(root, (r[7], r[1], r[3]))]
    validation = [r for r in validation
                  if _case_active(root, (r[7], r[1], r[3]))]
    # The historical cadence can be higher than this fresh cohort's first
    # training threshold. Start at batchMin; subsequent cycles still require
    # batchMin new training cases. No old gold contributes to this count.
    new_train = sum(row[10] is None for row in train)
    if len(train) < grant['cohort']['minTrainCases']:
        return {'status': 'waiting_fresh_train', 'train': len(train),
                'required': grant['cohort']['minTrainCases']}
    train_sha = _case_evidence(train)
    cycle_id = 'cohort-' + digest(canonical([
        grant['cohort']['id'], grant_sha, train_sha, EDIT_INPUT_VIEW,
        model_identity(policy['trainerArgv'][2])]))[:20]
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
                reserve_train(db, root, project, cycle_id)
            except ValueError as exc:
                return {'status': 'experiment_paused', 'reason': str(exc)[:80]}
            db.execute('INSERT INTO cycles VALUES (?,?,?,?,NULL)',
                (cycle_id, 'training', canonical({
                    'schema_version': 1, 'cohort_id': grant['cohort']['id'],
                    'authorizationSha256': grant_sha,
                    'trainEvidenceSha256': train_sha,
                    'model_input_view': EDIT_INPUT_VIEW}), time.time()))
        elif old[0] not in ('training', 'failed', 'staged', 'staged_evaluated'):
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
            trainer = _trainer(policy, trainer_factory)
            artifact.unlink(missing_ok=True)
            (folder / 'train.json').unlink(missing_ok=True)
            cases = [_case(row[:6]) for row in train]
            request = {'schema_version': 1,
                'train': [{k: v for k, v in case.items() if k != 'project_key'}
                          for case in cases],
                'artifact_path': str(artifact), 'manifest_sha256': train_sha,
                'model_input_view': EDIT_INPUT_VIEW}
            meta = {'job_id': cycle_id + ':train', 'parent_id': cycle_id,
                    'op': 'laya_train', 'runtime_kind': 'local_trainer',
                    'model_version': 'laya', 'source_version': 'base',
                    'destination_version': cycle_id}
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'queued', **meta)
            start = time.monotonic()
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'started',
                               queue_wait_ms=0, **meta)
            answer, train_resources = trainer.call('train', request,
                                                    folder / 'train.json', cwd=folder)
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
                if _case_evidence(latest) != train_sha:
                    stage['sourceActiveAtStage'] = False
                db.execute("UPDATE cycles SET status='staged',evidence=? "
                           "WHERE cycle_id=? AND status IN ('training','failed')",
                           (canonical(stage), cycle_id))
                if stage['sourceActiveAtStage']:
                    for row in train:
                        db.execute("UPDATE cases SET trained_cycle=? WHERE "
                            "project_key=? AND request_id=? AND input_sha256=? "
                            "AND status='gold' AND use_state='active' "
                            "AND trained_cycle IS NULL",
                            (cycle_id, row[0], row[1], row[2]))
            job_telemetry.emit(root / 'job-telemetry.jsonl', 'finished',
                status='success', run_ms=(time.monotonic() - start) * 1000,
                resources=train_resources, **meta)
            if not stage['sourceActiveAtStage']:
                return {'status': 'staged_source_revoked',
                        'cycle_id': cycle_id}
        except Exception as exc:
            if meta is not None and start is not None:
                job_telemetry.emit(root / 'job-telemetry.jsonl', 'finished',
                    status='failure', run_ms=(time.monotonic() - start) * 1000,
                    error_type=type(exc).__name__, **meta)
            with _db(root) as db:
                db.execute("UPDATE cycles SET status='failed',evidence=? "
                           "WHERE cycle_id=? AND status IN ('training','failed')",
                    (canonical({'reason': str(exc)[:80],
                                'authorizationSha256': grant_sha,
                                'trainEvidenceSha256': train_sha}), cycle_id))
            return {'status': 'failed', 'reason': str(exc)[:80],
                    'cycle_id': cycle_id}
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
    try:
        require_operation(root, policy, 'quality_evaluate',
                          approval_sha256=grant_sha)
    except ValueError:
        return {'status': 'staged_authorization_expired', 'cycle_id': cycle_id}
    if any(not _case_active(root, (r[7], r[1], r[3]))
           for r in train + validation):
        return {'status': 'staged_source_revoked', 'cycle_id': cycle_id}
    try:
        trainer = _trainer(policy, trainer_factory)
        cases = [_case(row[:6]) for row in validation]
        before, before_resources = _predict(trainer, folder, incumbent, cases,
                                            'staged-incumbent',
                                            input_view=EDIT_INPUT_VIEW)
        after, after_resources = _predict(trainer, folder, artifact, cases,
                                          'staged-candidate',
                                          input_view=EDIT_INPUT_VIEW)
        require_operation(root, policy, 'quality_evaluate',
                          approval_sha256=grant_sha)
        metrics = {'incumbent': before, 'candidate': after,
                   'validation_questions': len(validation),
                   'empty_validation': empty,
                   'evaluation_scope': 'fresh_candidate_disjoint_edit_cohort',
                   'applied': False,
                   'resources': {'incumbent': before_resources,
                                 'candidate': after_resources}}
        with _db(root) as db:
            latest_train, latest_validation = training_and_validation(
                db, project, grant)
            if (_case_evidence(latest_train) != train_sha
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
                'reason': str(exc)[:80]}
