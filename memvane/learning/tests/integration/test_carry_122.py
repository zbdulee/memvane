"""1.0.22 A-2: staged candidates across a real renewal, through the worker command.

The install is RenewalFixture's (real enrollment, grant, profile, policy and
the selector-maintenance renewal entry). Every pass is the real worker command
(``shared.main(['run-project', …])``). The trainer is a fake that writes the
candidate artifact; resource reports and the source-activity check are fixed.
"""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout
from unittest import mock

from memvane.learning.context_learning import (automatic_staging, candidate_apply, candidate_close,
                                               general_teacher, prequential, project_scope, runtime_authorization, shared)
from memvane.learning.context_learning.learning_queue import LearningQueue
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import edit_sample
from memvane.learning.tests.renewal_fixture import RenewalFixture


class FakeTrainer:
    def __init__(self, case):
        self.case = case

    def call(self, mode, request, result_path, *, cwd):
        if mode == 'train':
            self.case.trained.append(len(request['train']))
            path = request['artifact_path']
            with open(path, 'wb') as out:
                out.write(b'candidate-' + request['manifest_sha256'].encode())
            os.chmod(path, 0o600)
            return {'schema_version': 1, 'status': 'trained'}, {'fake': True}
        assert mode == 'predict', mode
        base = request['artifact_path'] == self.case.policy()['baseArtifact']
        style = self.case.base_style if base else self.case.candidate_style
        self.case.predicted.append(len(request['cases']))
        return {'predictions': [{'request_id': c['request_id'], 'input_sha256': c['input_sha256'],
                                 'selected_ids': self.case.gold_ids.get(c['request_id'], [])
                                 if style == 'gold' else []}
                                for c in request['cases']]}, {'fake': True}


class CarryFixture(RenewalFixture):
    teacher_off_switch = False

    def setUp(self):
        super().setUp()
        self.trained, self.predicted, self.recorded = [], [], []
        self.gold_ids = {}
        self.base_style, self.candidate_style = 'empty', 'gold'
        self.clock = time.time()
        self.due = mock.Mock(side_effect=shared.train_due)
        self.receipt()
        ready = {'ready': True, 'available_mib': 10000, 'load_per_cpu': 0.1, 'total_mib': 16384,
                 'query_status': {'memory_pressure': 'ok'},
                 'memory_pressure': {'level': 'normal', 'system_free_percent': 70}}
        report = mock.Mock(side_effect=lambda *a, **k: dict(ready, observed_at_mono=time.monotonic()))
        record = lambda project, event, **f: self.recorded.append(dict(f, event=event))
        for target, name, value in (
                (shared, 'resource_report', report),
                (general_teacher, 'resource_report', report),
                (shared, '_case_active', mock.Mock(return_value=True)),
                (general_teacher, '_case_active', mock.Mock(return_value=True)),
                (automatic_staging, '_trainer', mock.Mock(side_effect=lambda *a, **k: FakeTrainer(self))),
                (candidate_apply, 'service_transport', mock.Mock(side_effect=lambda *a: self.transport)),
                (prequential, '_emit', mock.Mock(side_effect=record)),
                (general_teacher, '_emit', mock.Mock(side_effect=record)),
                (prequential, 'recall_selection', mock.Mock(return_value=None)),
                (prequential, 'recall_selections', mock.Mock(return_value={})),
                (shared, 'train_due', self.due)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def receipt(self):
        """The warm receipt the mocked selector warm would have written for the base pointer."""
        from memvane.learning.context_learning import inference_identity, learning_profile
        pointer = json.loads((self.root / 'active-checkpoint.json').read_text())
        profile = self.read(learning_profile.NAME)['proposal']
        receipt = {'schema': inference_identity.SCHEMA, 'view': profile['view'],
                   'verified': {'base_model_sha256': profile['modelIdentitySha256']},
                   'checkpoint_sha256': learning_profile.digest(pointer), 'createdAt': 1}
        for name, value in ((inference_identity.RECEIPT, receipt),
                            (inference_identity.BINDING, {'schema': inference_identity.SCHEMA,
                                                          'receipt_sha256': learning_profile.digest(receipt)})):
            if not (self.root / name).exists():
                self.write(self.root / name, value)

    def transport(self, command, record):
        return {'status': 'prepared' if command == 'prepare' else 'activated', 'token': record['apply_id']}

    def policy(self):
        return shared._policy(self.root)

    def grant(self):
        return project_scope.require(self.root, self.policy(), 'local_train')

    def approval(self):
        return runtime_authorization.proposal_sha256(self.grant())

    def gold(self, rid, *, empty=False):
        sample, bodies = edit_sample(rid)
        selected = [] if empty else [sample['candidates'][0]['candidate_id']]
        self.clock += 1
        value = {'selected_ids': selected, 'input_sha256': digest(canonical(sample)), 'review': 'single_pass'}
        with shared._db(self.root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (shared._project_key(self.project), str(self.project), rid,
                        digest(canonical(sample)), canonical(sample), canonical(bodies), 'corpus',
                        self.clock, 'train', '[]', 'active', 'gold', canonical(value)))
        self.gold_ids[shared._project_key(self.project)[:12] + ':' + rid] = selected

    def golds(self, prefix, count, *, empty=0):
        for index in range(count):
            self.gold(f'{prefix}-{index:03d}', empty=index < empty)

    def worker(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(shared.main(['run-project', str(self.project)]), 0, out.getvalue())
        return json.loads(out.getvalue())

    def jobs(self):
        with shared._db(self.root) as db:
            return [dict(json.loads(m), _approval=a) for m, a in
                    db.execute('SELECT manifest,approval FROM learning_jobs ORDER BY rowid')]

    def cycle_rows(self):
        with shared._db(self.root) as db:
            return dict(db.execute('SELECT cycle_id,status FROM cycles'))

    def evaluations(self):
        with shared._db(self.root) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_evaluations'").fetchone():
                return {}
            return {row[0]: row[1:] for row in db.execute(
                'SELECT cycle_id,status,reason FROM learning_evaluations')}

    def renew(self):
        before = self.approval()
        self.select()
        self.maintenance()
        after = self.approval()
        self.assertNotEqual(before, after)
        return before, after


    def legacy_job(self, count):
        """A 1.0.21-shaped staged job (no ``lineage``) of the first ``count`` gold, at the current approval."""
        grant, policy = self.grant(), self.policy()
        with shared._db(self.root) as db:
            rows = prequential.training_rows(db, self.project, grant)[:count]
        queue = LearningQueue(self.root)
        approval = runtime_authorization.proposal_sha256(grant)
        job = queue.plan(rows, approval=approval, cohort=grant['cohort']['id'],
                         family=automatic_staging.job_family(policy), minimum=5, maximum=128,
                         roles=['train'] * len(rows), cumulative=True)
        self.assertNotIn('lineage', job)
        queue.complete(job['id'])
        folder = self.root / 'cycles' / job['id']
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / 'cycles').chmod(0o700)
        artifact = folder / 'candidate.artifact'
        artifact.write_bytes(b'legacy candidate')
        artifact.chmod(0o600)
        stage = {'schema_version': 1, 'cohort_id': grant['cohort']['id'], 'authorizationSha256': approval,
                 'trainEvidenceSha256': automatic_staging._case_evidence(rows),
                 'artifactPath': str(artifact), 'artifactSha256': shared._hash(artifact),
                 'baseArtifactSha256': shared._hash(policy['baseArtifact']),
                 'model_input_view': 'tool-edit-8192-choice-v1',
                 'trainCases': [{'projectRoot': r[7], 'requestId': r[1], 'inputSha256': r[2]} for r in rows],
                 'sourceActiveAtStage': True}
        with shared._db(self.root) as db:
            db.execute("INSERT INTO cycles VALUES (?,'staged',?,?,NULL)", (job['id'], canonical(stage), time.time()))
        return job['id']

    def promotions(self, outcome):
        return [r for r in self.recorded if r['event'] == 'learning.promotion' and r.get('outcome') == outcome]

    def frozen(self):
        """100 gold train one promotion candidate (with lineage) that waits for evaluation."""
        self.golds('t', 100, empty=20)
        result = self.worker()
        self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort', result)
        self.assertEqual(self.trained, [100])
        job = self.jobs()[-1]
        self.assertEqual(job['lineage'], candidate_close.lineage(self.grant()))
        return job


class Carry(CarryFixture):
    def test_t2_1_t2_2_candidate_carried_across_real_renewal_then_judged(self):
        job = self.frozen()
        self.golds('v', 30, empty=10)
        before = self.worker()['cycle']
        self.assertEqual((before['cycle_id'], before['validation']), (job['id'], 30))
        old, new = self.renew()
        result = self.worker()['cycle']
        self.assertEqual((result['status'], result['cycle_id'], result['validation']),
                         ('staged_waiting_quality_cohort', job['id'], 30))
        self.assertEqual(self.trained, [100])
        moved = self.jobs()[-1]
        self.assertEqual((moved['id'], moved['_approval'], moved['approval']), (job['id'], new, new))
        with shared._db(self.root) as db:
            stage = json.loads(db.execute('SELECT evidence FROM cycles WHERE cycle_id=?', (job['id'],)).fetchone()[0])
        self.assertEqual((stage['authorizationSha256'], stage['carriedFrom']), (new, [old]))
        self.assertEqual(len(self.promotions('carried')), 1)
        self.golds('w', 20)
        result = self.worker()['cycle']
        self.assertEqual((result['status'], result['cycle_id']), ('applied', job['id']), result)
        self.assertEqual(self.trained, [100])
        self.assertEqual(self.predicted, [50, 50])

    def test_t2_4_another_job_family_is_not_carried_and_a_new_job_trains(self):
        job = self.frozen()
        self.renew()
        # A new model or base artifact comes with a new setup grant whose non-binding
        # fields (the lineage) can be the same; the job family is what differs.
        self.golds('n', 5)
        with mock.patch.object(automatic_staging, 'job_family', return_value='f' * 64):
            result = self.worker()
        self.assertEqual(self.evaluations()[job['id']][:2], ('blocked', 'authorization_superseded'))
        self.assertEqual(len(self.trained), 2, result)
        self.assertNotEqual(self.jobs()[-1]['id'], job['id'])

    def test_t2_5_changed_candidate_artifact_is_not_carried(self):
        job = self.frozen()
        self.renew()
        artifact = self.root / 'cycles' / job['id'] / 'candidate.artifact'
        artifact.write_bytes(b'other bytes')
        self.worker()
        self.assertEqual(self.evaluations()[job['id']][:2], ('blocked', 'authorization_superseded'))
        self.assertEqual(self.promotions('carried'), [])

    def test_t2_6_an_interrupted_carry_leaves_nothing_and_the_rerun_carries(self):
        job = self.frozen()
        old, new = self.renew()
        before = (self.jobs(), self.cycle_rows(), self.evaluations())
        real = prequential._has
        calls = []

        def interrupted(db, table):
            if table == 'learning_evaluations' and calls is not None:
                calls.append(table)
                if len(calls) == 3:
                    raise RuntimeError('synthetic interruption')
            return real(db, table)
        with mock.patch.object(prequential, '_has', side_effect=interrupted):
            with self.assertRaises(Exception):
                candidate_close.supersede(self.root, self.policy(), self.project)
        self.assertEqual((self.jobs(), self.cycle_rows(), self.evaluations()), before)
        result = self.worker()['cycle']
        self.assertEqual(result['cycle_id'], job['id'])
        self.assertEqual(self.jobs()[-1]['_approval'], new)

    def test_t2_10_unfinished_application_or_rejection_is_not_carried(self):
        job = self.frozen()
        self.renew()
        grant = self.grant()
        for phase in ('intent', 'prepared', 'rejected'):
            with self.subTest(phase=phase):
                path = self.root / candidate_apply.JOURNAL
                self.write(path, {'cycle_id': job['id'], 'phase': phase, 'apply_id': 'f' * 32})
                with mock.patch.object(candidate_apply, 'read_journal',
                                       side_effect=lambda root: json.loads(path.read_text())):
                    self.assertIsNone(candidate_close.carry_candidate(self.root, self.policy(), grant, self.project))
                path.unlink()
        self.assertIsNotNone(candidate_close.carry_candidate(self.root, self.policy(), grant, self.project))

    def test_t2_11_an_expired_grant_closes_nothing(self):
        self.frozen()
        self.renew()
        before = (self.jobs(), self.cycle_rows(), self.evaluations())
        policy = self.policy()
        with mock.patch('time.time', return_value=time.time() + 20 * 365 * 86400):
            self.assertIsNone(candidate_close.supersede(self.root, policy, self.project))
        self.assertEqual((self.jobs(), self.cycle_rows(), self.evaluations()), before)


class Lineage(CarryFixture):
    def test_t2_3_another_lineage_is_not_carried_and_is_closed(self):
        job = self.frozen()
        self.renew()
        grant = self.grant()
        variants = {'cohort': dict(grant, cohort=dict(grant['cohort'], id='other-cohort')),
                    'operations': dict(grant, operations=list(grant['operations'])[:-1]),
                    'projectRoot': dict(grant, projectRoot=str(self.home))}
        for name, other in variants.items():
            with self.subTest(field=name):
                self.assertNotEqual(candidate_close.lineage(other), candidate_close.lineage(grant))
                self.assertNotEqual(candidate_close.lineage(other), job['lineage'])
        # Only the binding differs from the renewed grant: same lineage.
        self.assertEqual(job['lineage'], candidate_close.lineage(grant))
        with mock.patch.object(candidate_close, 'lineage', return_value='0' * 64):
            self.worker()
        self.assertEqual(self.evaluations()[job['id']][:2], ('blocked', 'authorization_superseded'))
        self.assertEqual(self.promotions('carried'), [])
        self.assertEqual(len(self.promotions('superseded')), 1)


class LegacyCycles(CarryFixture):
    """T2-7 and T2-9: the reproduced stall, reversed (1.0.21-shaped cycles have no lineage)."""

    def check(self, count):
        self.golds('g', count, empty=min(20, count // 5))
        cycle = self.legacy_job(count)
        self.assertEqual(self.cycle_rows()[cycle], 'staged')
        self.renew()
        self.golds('h', 5)
        due = self.due.call_count
        result = self.worker()
        self.assertEqual(self.due.call_count, due + 1, result)
        self.assertEqual(self.evaluations()[cycle][:2], ('blocked', 'authorization_superseded'))
        return result

    def test_t2_9_small_legacy_cycle_no_longer_stops_training(self):
        result = self.check(5)
        self.assertEqual(result['cycle'], {'status': 'waiting_fresh_train', 'train': 10, 'required': 100})
        self.assertEqual(self.trained, [])

    def test_t2_7_t2_9_promotion_sized_legacy_cycle_is_closed_and_the_next_job_trains(self):
        result = self.check(100)
        self.assertEqual(result['cycle']['status'], 'staged_waiting_quality_cohort', (result, self.jobs()[-1]['members'][:2], len(self.jobs()), [ (j['id'], j['_approval'][:8], len(j['members'])) for j in self.jobs()]))
        self.assertEqual(self.trained, [105])
        self.assertEqual(self.promotions('carried'), [])


class Prequential(CarryFixture):
    """A-4 and A-5 through the worker command."""

    def test_t4_1_no_training_before_one_hundred_gold(self):
        for count in (5, 50, 99):
            self.golds('g%d' % count, count - len(self.gold_ids))
            self.assertEqual(self.worker()['cycle']['status'], 'waiting_fresh_train')
        self.assertEqual(self.trained, [])
        self.gold('g-last')
        self.worker()
        self.assertEqual(self.trained, [100])

    def test_t4_2_duplicates_below_one_hundred_unique_plan_no_job(self):
        from memvane.learning.tests.fake_teacher_cli_121 import edit_sample
        self.golds('g', 99)
        sample, bodies = edit_sample('dup', text='Field notes g-000: the author keeps writing about the garden.')
        self.clock += 1
        value = {'selected_ids': [sample['candidates'][0]['candidate_id']],
                 'input_sha256': digest(canonical(sample)), 'review': 'single_pass'}
        with shared._db(self.root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (shared._project_key(self.project), str(self.project), 'dup',
                        digest(canonical(sample)), canonical(sample), canonical(bodies), 'corpus',
                        self.clock, 'train', '[]', 'active', 'gold', canonical(value)))
        result = self.worker()['cycle']
        self.assertEqual(result['status'], 'waiting_fresh_batch', result)
        self.assertEqual((self.jobs(), self.trained), ([], []))

    def test_t4_4_legacy_small_job_at_the_same_approval_trains_nothing_until_one_hundred(self):
        self.golds('g', 10)
        cycle = self.legacy_job(10)
        for _ in range(2):
            self.worker()
        self.assertEqual(self.trained, [])
        self.golds('h', 90)
        self.worker()
        self.assertEqual(len(self.trained), 1)
        self.assertNotEqual(self.jobs()[-1]['id'], cycle)

    def test_t5_1_t5_3_replay_takes_the_newest_consumed_cases(self):
        self.golds('a', 100)
        self.worker()
        # Close each candidate so the next job is planned from the replay.
        prequential.close(self.root, self.jobs()[-1]['id'], 'fixture_closed')
        self.golds('b', 90)
        self.worker()
        prequential.close(self.root, self.jobs()[-1]['id'], 'fixture_closed')
        self.golds('c', 10)
        self.worker()
        latest = self.jobs()[-1]
        members = [m['request_id'] for m in latest['members']]
        names = [f'a-{i:03d}' for i in range(100)] + [f'b-{i:03d}' for i in range(90)] + [f'c-{i:03d}' for i in range(10)]
        self.assertEqual(len(members), 128)
        self.assertEqual(sorted(members), sorted(names[-128:]))
        with shared._db(self.root) as db:
            rows = prequential.training_rows(db, self.project, self.grant())
        newest = max(r[11] for r in rows if r[1] in members)
        self.assertEqual(prequential.cutoff(rows, latest), newest)
        self.assertEqual(self.trained, [100, 128, 128])


if __name__ == '__main__':
    import unittest
    unittest.main()

