"""Disposable normal-scheduler staged-training proof; no live models or providers.

Cohort admission is stubbed with a fixed train/holdout split by request ID;
`general_cohort` owns real admission and has its own checks.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import (automatic_cohort, automatic_staging, shared,
                              project_scope, resource_coordination)
from context_learning.store import canonical
from context_learning.contracts import digest


with tempfile.TemporaryDirectory(prefix='qmd-auto-stage-', dir='/tmp') as tmp:
    project = Path(tmp).resolve() / 'project'; project.mkdir(mode=0o700)
    root = project / '.shared-learning'; root.mkdir(mode=0o700)
    base = root / 'base.artifact'; base.write_bytes(b'base'); base.chmod(0o600)
    model = root / 'model'; model.mkdir(mode=0o700)
    cutoff = 5000
    train_prefixes = [f'docs/train-{n}/' for n in range(5)]
    validation_prefixes = [f'docs/validation-{n}/' for n in range(5)]
    cohort = {'schema': project_scope.COHORT_SCHEMA,
        'id': 'staging-test-v1', 'notBeforeUnixMs': cutoff * 1000,
        'minTrainCases': 5, 'minQualityTrainCases': 100,
        'minTrainFamilies': 5, 'minValidationCases': 50,
        'minValidationFamilies': 5,
        'minEmptyValidation': 10}
    grant = {'scope': project_scope.SCOPE, 'cohort': cohort,
        'projectRoot': str(project),
        'operations': ['gold', 'local_train', 'quality_evaluate']}
    policy = {'schema': shared.SCHEMA, 'enabled': True,
        '_runtime_authorization_v2': True,
        'modelInputView': 'tool-edit-8192-choice-v1',
        'trainerArgv': ['/fake/python', '/fake/adapter', str(model)],
        'baseArtifact': str(base), 'batchMin': 5, 'batchMax': 120,
        'safetySeconds': 900, 'maxRssMib': 4096}
    coord=root/'coordination';coord.mkdir(mode=0o700)
    dbpath = root / shared.DATABASE_NAME
    with shared._db_at(dbpath):
        pass

    @contextmanager
    def local_db(_):
        with shared._db_at(dbpath) as db:
            yield db

    def insert(rid, cid, path, at, *, split='train', status='gold',
               selected=True):
        cid = 'project-wiki/' + cid + '.md'
        sample = {'schema_version': 4, 'request_id': rid,
            'prompt': {'text': 'technical question ' + rid},
            'candidates': [{'candidate_id': cid,
                            'revision_sha256': digest(cid)}],
            'edit_event': {'ticket_id': 'ticket-' + rid}}
        raw = canonical(sample); input_sha = digest(raw)
        gold = canonical({'selected_ids': [cid] if selected else [],
                          'input_sha256': input_sha}) if status == 'gold' else None
        key = 'project-key'
        families = canonical(['target:' + digest(canonical(
            [key, 'edit:' + path]))])
        with shared._db_at(dbpath) as db:
            db.execute("INSERT INTO cases (project_key,project_root,request_id,"
                "input_sha256,sample,bodies,corpus_fingerprint,captured_at,split,"
                "family_keys,use_state,status,gold) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, str(project), rid, input_sha, raw,
                 canonical({cid: 'body ' + cid}), 'index', at, split,
                 families,
                 'active', status, gold))

    def admitted(db, project_root, scoped_grant, statuses=('queued', 'first_done')):
        """Fixed split: `train-N` trains, `validation-N` holds out, others never admit."""
        assert scoped_grant['scope'] == project_scope.SCOPE
        rows = db.execute(
            "SELECT project_key,request_id,input_sha256,sample,bodies,gold,split,"
            "project_root,corpus_fingerprint,family_keys,trained_cycle,captured_at,"
            "status,use_state FROM cases WHERE project_root=? "
            "ORDER BY captured_at,request_id", (str(project_root),)).fetchall()
        result = []
        for row in rows:
            if row[11] < cutoff or row[12] not in statuses or row[13] != 'active':
                continue
            name, _, number = row[1].rpartition('-')
            if name not in ('train', 'validation'):
                continue
            role = 'train' if name == 'train' else 'holdout'
            # One exposure family per case, as distinct project sources give.
            result.append((role, tuple(row[:12]) + (f'{role}/{number}',)))
        return result

    insert('old-queued', 'legacy-card',
           train_prefixes[0] + 'old.md', 4000,
           status='queued')
    insert('old-awaiting', 'legacy-awaiting',
           train_prefixes[0] + 'awaiting.md', 4000.1,
           status='awaiting_local')
    insert('old-pending', 'legacy-pending',
           train_prefixes[0] + 'pending.md', 4000.2,
           status='pending_review')
    insert('sealed-eval', 'sealed-card',
           train_prefixes[0] + 'sealed-target.md', 4001,
           split='final')
    insert('new-unadmitted', 'unadmitted-card',
           train_prefixes[0] + 'unadmitted.md', 5001)
    for n in range(5):
        insert('train-' + str(n), 'fresh-train-' + str(n),
               train_prefixes[n % 5] + 'source-' + str(n) + '.md',
               5002 + n)
    admission = patch.object(automatic_cohort, 'admitted', side_effect=admitted)
    admission.start()

    calls = []
    runtime_state = {'revoked': False, 'revoke_during_train': False,
                     'source_active': True,
                     'revoke_source_during_train': False}
    class FakeTrainer:
        def call(self, mode, request, result_path, *, cwd):
            assert mode == 'train'
            assert len(self.inherited_fds)==2
            assert all(os.fstat(fd).st_nlink==1 for fd in self.inherited_fds)
            assert request['learning_control']['root']==str(coord)
            calls.append(request)
            Path(request['artifact_path']).write_bytes(b'candidate-' +
                request['manifest_sha256'].encode())
            Path(request['artifact_path']).chmod(0o600)
            if runtime_state['revoke_during_train']:
                runtime_state['revoked'] = True
            if runtime_state['revoke_source_during_train']:
                runtime_state['source_active'] = False
            return {'schema_version': 1, 'status': 'trained'}, {'fake': True}

    def operation(_root, _policy, op, *, approval_sha256=None):
        if runtime_state['revoked'] or op not in grant['operations']:
            raise ValueError('runtime_authorization_required')
        return grant

    ready = {'ready': True, 'available_mib': 10000,
             'load_per_cpu': 0.1, 'total_mib':16384,'observed_at_mono':time.monotonic(),
             'query_status':{'memory_pressure':'ok'},
             'memory_pressure':{'level':'normal','system_free_percent':70}}
    def fresh_ready(*_args, **_kwargs):
        # This synthetic fixture can run for more than the two-second runtime
        # observation lifetime on CI. Keep each mocked observation fresh.
        return {**ready, 'observed_at_mono': time.monotonic()}
    with patch.object(resource_coordination,'default_root',return_value=coord), patch.object(shared, '_db', local_db), patch.object(
            shared, '_case_active',
            side_effect=lambda *_args: runtime_state['source_active']), patch.object(
            shared, 'resource_report', side_effect=fresh_ready), patch.object(
            shared, '_historical_trainer_policy', return_value=False), patch.object(
            automatic_staging, 'require_operation', side_effect=operation), patch(
            'context_learning.runtime_authorization.require_operation',
            side_effect=operation), patch(
            'context_learning.laya_adapter.model_identity',
            return_value='local-model-identity'):
        from context_learning.local_cycle import ResourceDeferred
        from context_learning import learning_profile
        with patch.object(learning_profile,'present',return_value=True),patch.object(
                learning_profile,'require',return_value={'localTrain':True,'minFreshTrainCases':100}):
            waiting=shared.train_due(root,policy,trainer_factory=lambda _:FakeTrainer())
        assert waiting=={'status':'waiting_fresh_batch','threshold':100},waiting
        assert not calls
        with patch.object(shared, 'resource_report', return_value={
                'ready': False, 'memory_admission': {
                    'reason': 'minimum_microbatch_unavailable'}}):
            deferred = shared.train_due(root, policy,
                trainer_factory=lambda _: FakeTrainer())
        assert deferred['status'] == 'deferred_resource', deferred
        receipt = json.loads((root / 'resource-defer.json').read_text())
        assert receipt['completionAck'] is False and not calls
        with shared._db_at(dbpath) as db:
            assert db.execute('SELECT COUNT(*) FROM cycles').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM cases WHERE trained_cycle IS NOT NULL').fetchone()[0] == 0
        # Advance the durable retry eligibility in this disposable fixture.
        with shared._db_at(dbpath) as db:
            assert db.execute("SELECT retry_at FROM learning_jobs").fetchone()[0] > 0
            db.execute("UPDATE learning_jobs SET retry_at=0")
        class DeferredTrainer:
            def call(self, *args, **kwargs):
                raise ResourceDeferred('trainer_system_pressure', {'ready':False})
        deferred = shared.train_due(root, policy,
            trainer_factory=lambda _: DeferredTrainer())
        assert deferred['status'] == 'deferred_resource', deferred
        with shared._db_at(dbpath) as db:
            assert db.execute('SELECT status FROM cycles').fetchone()[0] == 'deferred'
            assert db.execute('SELECT COUNT(*) FROM cases WHERE trained_cycle IS NOT NULL').fetchone()[0] == 0
        assert not (root / 'experimental-checkpoint.json').exists()
        with shared._db_at(dbpath) as db:
            db.execute("UPDATE learning_jobs SET retry_at=0")
        result = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert result['status'] == 'staged_waiting_quality_cohort', result
        assert len(calls) == 1 and len(calls[0]['train']) == 5
        assert not (root / 'experimental-checkpoint.json').exists()
        with patch.object(shared, '_policy', return_value=policy), patch.object(
                shared, 'sync_reviewed',
                side_effect=AssertionError('unscoped historical sync')), patch.object(
                shared, '_run_impl',
                return_value={'status': 'staged_waiting_quality_cohort'}):
            assert shared.run(root, project=project,
                sync_config={})['status'] == 'staged_waiting_quality_cohort'
        with shared._db_at(dbpath) as db:
            assert db.execute("SELECT status FROM cases WHERE request_id='old-queued'").fetchone()[0] == 'queued'
            assert db.execute("SELECT status FROM cases WHERE request_id='old-awaiting'").fetchone()[0] == 'awaiting_local'
            assert db.execute("SELECT status FROM cases WHERE request_id='old-pending'").fetchone()[0] == 'pending_review'
            assert db.execute("SELECT trained_cycle FROM cases WHERE request_id='sealed-eval'").fetchone()[0] is None
            assert db.execute("SELECT status FROM cycles").fetchone()[0] == 'staged'
        again = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert again['status'] == 'staged_waiting_quality_cohort' and len(calls) == 1

        runtime_state['revoked'] = True
        blocked = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert blocked['status'] == 'runtime_approval_required'
        assert not (root / 'experimental-checkpoint.json').exists()
        runtime_state['revoked'] = False

        for n in range(50):
            insert('validation-' + str(n), 'fresh-validation-' + str(n),
                   validation_prefixes[n % 5] + 'source-' + str(n) + '.md',
                   5010 + n, split='train', selected=n >= 10)
        waiting_for_train = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert waiting_for_train['status'] == 'staged_waiting_quality_cohort'
        assert waiting_for_train['required_train'] == 100
        assert waiting_for_train['validation_families'] == 50
        assert len(calls) == 1
        # Available historical labels cannot satisfy this head's train gate.
        with shared._db_at(dbpath) as db:
            existing_train, existing_validation = automatic_cohort.training_and_validation(db,project,grant)
        historical=[]
        for n in range(95):
            row=list(existing_train[n%len(existing_train)])
            row[1]='historical-'+str(n);row[10]='older-trained-head'
            historical.append(tuple(row))
        with patch.object(automatic_staging,'training_and_validation',return_value=(existing_train+historical,existing_validation)), patch.object(shared,'_predict',side_effect=AssertionError('untrained cohort satisfied quality gate')):
            bounded=shared.train_due(root,policy,trainer_factory=lambda _:FakeTrainer())
        assert bounded['status']=='staged_waiting_quality_cohort' and bounded['train']==5,bounded
        for n in range(5, 100):
            insert('train-' + str(n), 'fresh-train-' + str(n),
                   train_prefixes[n % 5] + 'source-' + str(n) + '.md',
                   5100 + n)
        def predict(_trainer, _folder, _artifact, cases, _label,
                    *, input_view=None,cache_epoch=None):
            return {'questions': len(cases), 'exact': len(cases),
                    'exact_rate': 1.0, 'projects': {}}, {'fake': True}
        with patch.object(shared, '_predict', side_effect=ResourceDeferred('fixture memory')):
            deferred_evaluation = shared.train_due(root, policy,trainer_factory=lambda _:FakeTrainer())
        assert deferred_evaluation['status']=='staged_evaluation_deferred',deferred_evaluation
        assert hasattr(automatic_staging,'evaluate_pending'),'quality-only recovery missing'
        with shared._db_at(dbpath) as db:
            assert db.execute("SELECT COUNT(*) FROM learning_evaluations WHERE status='deferred'").fetchone()[0]==1
            db.execute('UPDATE learning_evaluations SET retry_at=0')
        def quality_only(_root,_policy,op,**kwargs):
            assert op=='quality_evaluate','evaluation requested training or gold permission'
            return grant
        with patch.object(shared, '_predict', side_effect=predict),patch.object(automatic_staging,'require_operation',side_effect=quality_only):
            evaluated=automatic_staging.evaluate_pending(root,policy,deferred_evaluation['cycle_id'],trainer_factory=lambda _:FakeTrainer())
        assert evaluated['status'] == 'staged_evaluated', evaluated
        assert evaluated['metrics']['applied'] is False
        assert evaluated['metrics']['empty_validation'] == 10
        assert len(calls) == 2
        assert not (root / 'experimental-checkpoint.json').exists()
        try:
            shared._publish_evaluated(root, evaluated['cycle_id'],
                {'authorizationSha256': 'bound'}, [])
        except ValueError as exc:
            assert str(exc) == 'automatic_cohort_publication_not_authorized'
        else:
            raise AssertionError('staged cohort entered pointer publication')
        with shared._db_at(dbpath) as db:
            assert db.execute("SELECT status FROM cycles WHERE cycle_id=?",
                (evaluated['cycle_id'],)).fetchone()[0] == 'staged_evaluated'

        for n in range(100, 105):
            insert('train-' + str(n), 'fresh-train-' + str(n),
                   train_prefixes[n % 5] + 'source-' + str(n) + '.md',
                   5500 + n)
        runtime_state['revoke_during_train'] = True
        interrupted = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert interrupted['status'] == 'staged_authorization_expired', interrupted
        assert len(calls) == 3
        assert not (root / 'experimental-checkpoint.json').exists()
        with shared._db_at(dbpath) as db:
            assert db.execute("SELECT COUNT(*) FROM cycles WHERE status='staged'").fetchone()[0] == 2
        runtime_state['revoked'] = False
        runtime_state['revoke_during_train'] = False

        for n in range(105, 110):
            insert('train-' + str(n), 'fresh-train-' + str(n),
                   train_prefixes[n % 5] + 'source-' + str(n) + '.md',
                   5600 + n)
        runtime_state['revoke_source_during_train'] = True
        source_changed = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert source_changed['status'] == 'staged_source_revoked', source_changed
        assert len(calls) == 4
        assert (root / 'cycles' / source_changed['cycle_id'] /
                'candidate.artifact').exists()
        assert not (root / 'experimental-checkpoint.json').exists()
        runtime_state['source_active'] = True
        runtime_state['revoke_source_during_train'] = False
        same = shared.train_due(root, policy,
            trainer_factory=lambda _: FakeTrainer())
        assert same['status'] == 'staged_source_revoked' and len(calls) == 4

    admission.stop()

    print(json.dumps({'staged_train': True, 'backlog_preserved': True,
        'evaluation_no_pointer': True, 'revoke_blocks': True,
        'source_revocation_stages_without_evaluation': True}))
