"""1.0.22 A-3 and A-8: a judged candidate is closed in both profiles; heal waits for the enable fence.

Explicit-profile passes go through the worker entry (``project_worker.run``)
with the explicit teacher stubbed and a fixed train/validation split standing
in for the explicit cohort assignment (``t-*`` train, ``v-*`` validation).
The learning and evaluation queues, staging, evaluation and candidate
application are real; the trainer is ``FakeTrainer``.
"""
from __future__ import annotations

import json
from unittest import mock

from memvane.learning.context_learning import (automatic_cohort, automatic_staging, candidate_apply,
                                               general_teacher, learning_profile, prequential,
                                               project_scope, runtime_authorization, selector_heal,
                                               shared)
from memvane.learning.tests.integration.test_worker_teacher_121 import WorkerFixture
from memvane.learning.tests.renewal_fixture import RenewalFixture


class ApplyCounter(WorkerFixture):
    def setUp(self):
        super().setUp()
        real = candidate_apply.apply_candidate
        self.applied = []

        def apply(root, policy, cycle_id, **kwargs):
            self.applied.append(cycle_id)
            return real(root, policy, cycle_id, **kwargs)
        self.patches.enter_context(mock.patch.object(candidate_apply, 'apply_candidate', side_effect=apply))
        self.due = mock.Mock(side_effect=shared.train_due)
        self.patches.enter_context(mock.patch.object(shared, 'train_due', self.due))

    def evaluation(self, cycle):
        with shared._db(self.root) as db:
            row = db.execute('SELECT status,reason FROM learning_evaluations WHERE cycle_id=?', (cycle,)).fetchone()
        return tuple(row) if row else None


class Explicit(ApplyCounter):
    def setUp(self):
        super().setUp()
        self.kind = 'explicit'
        self.grant = dict(self.grant, operations=list(project_scope.OPERATIONS))
        self.profile['runtimeApprovalSha256'] = runtime_authorization.proposal_sha256(self.grant)
        self.weekly.side_effect = None
        self.weekly.return_value = {'remainingPercent': 80}
        self.base_style, self.candidate_style = 'gold', 'empty'
        for target, name, value in (
                (general_teacher, 'process_pending', mock.Mock(return_value={'status': 'drained'})),
                (automatic_staging, 'training_and_validation', mock.Mock(side_effect=self.split)),
                (automatic_cohort, 'training_and_validation', mock.Mock(side_effect=self.split))):
            self.patches.enter_context(mock.patch.object(target, name, value))

    def split(self, db, project, grant, job=None):
        rows = prequential.training_rows(db, project, grant)
        return ([r for r in rows if not r[1].startswith('v-')], [r for r in rows if r[1].startswith('v-')])

    def test_t3_1_judged_candidate_is_never_applied_again_and_the_next_job_trains(self):
        self.golds('t', 100, empty=20)
        self.golds('v', 50, empty=10)
        first = self.worker()['cycle']
        self.assertEqual((first['status'], first['reason']), ('apply_blocked', 'apply_quality_regression'))
        cycle = first['cycle_id']
        self.assertEqual(self.applied, [cycle])
        self.assertEqual(self.evaluation(cycle), ('blocked', 'apply_quality_regression'))
        self.assertEqual(self.worker()['cycle']['status'], 'waiting_fresh_batch')
        self.assertEqual(self.applied, [cycle])
        self.golds('u', 5)
        trained = len(self.trained)
        self.worker()
        self.assertEqual(len(self.trained), trained + 1)
        self.assertEqual(len(self.jobs()), 2)
        self.assertNotIn(cycle, self.applied[1:])

    def test_t3_1_an_older_judged_candidate_is_not_picked_after_the_newer_one_closes(self):
        self.golds('t', 100, empty=20)
        self.worker()
        self.golds('u', 5)
        self.worker()
        older, newer = [job['id'] for job in self.jobs()]
        # The shape a pre-1.0.22 refusal left: evaluated, completed and still open.
        with shared._db(self.root) as db:
            db.execute("UPDATE cycles SET status='staged_evaluated' WHERE cycle_id=?", (older,))
            db.execute("UPDATE learning_evaluations SET status='completed' WHERE cycle_id=?", (older,))
        self.golds('v', 50, empty=10)
        result = self.worker()['cycle']
        self.assertEqual((result['cycle_id'], result['reason']), (newer, 'apply_quality_regression'))
        self.assertEqual(self.evaluation(older), ('blocked', 'superseded_by_judged'))
        for _ in range(2):
            self.worker()
        self.assertEqual(self.applied, [newer])

    def test_t3_2_a_stale_quality_refusal_is_not_closed(self):
        self.golds('t', 100, empty=20)
        self.golds('v', 50, empty=10)
        stale = lambda root, policy, cycle_id, **k: {'status': 'apply_blocked', 'reason': 'apply_quality_stale',
                                                     'cycle_id': cycle_id}
        with mock.patch.object(candidate_apply, 'apply_candidate', side_effect=stale) as apply:
            cycle = self.worker()['cycle']['cycle_id']
            self.assertEqual(self.evaluation(cycle)[0], 'completed')
            self.worker()
        self.assertEqual([c.args[2] for c in apply.call_args_list], [cycle, cycle])

    def test_t3_4_failure_after_the_intent_closes_then_new_evidence_reaches_training(self):
        self.golds('t', 100, empty=20)
        self.golds('v', 50, empty=10)
        self.base_style, self.candidate_style = 'empty', 'gold'
        self.transport = lambda command, record: {'status': 'failed'}
        first = self.worker()['cycle']
        self.assertEqual((first['status'], first['reason']), ('apply_blocked', 'apply_prepare_failed'))
        cycle = first['cycle_id']
        self.assertEqual(candidate_apply.read_journal(self.root)['phase'], 'rejected')
        self.assertEqual(self.evaluation(cycle), ('blocked', 'candidate_rejected'))
        # New gold changes the evaluation evidence before the next pass.
        self.golds('v2', 3)
        due = self.due.call_count
        result = self.worker()['cycle']
        self.assertEqual(self.applied, [cycle])
        self.assertEqual(self.due.call_count, due + 1)
        self.assertEqual(result['status'], 'waiting_fresh_batch')


    def test_t2_8_reapproval_closes_the_earlier_cycle_and_the_next_job_trains(self):
        self.golds('t', 100, empty=20)
        self.worker()
        first = self.jobs()[-1]
        self.assertNotIn('lineage', first)
        self.assertEqual(self.trained and len(self.trained), 1)
        # A new explicit approval (its own setup) with the same cases.
        self.grant = dict(self.grant, expiresAtUnixMs=self.grant.get('expiresAtUnixMs', 0) + 1)
        self.profile['runtimeApprovalSha256'] = runtime_authorization.proposal_sha256(self.grant)
        self.golds('u', 5)
        self.worker()
        self.assertEqual(self.evaluation(first['id']), ('blocked', 'authorization_superseded'))
        self.assertEqual(len(self.trained), 2)
        self.assertNotEqual(self.jobs()[-1]['id'], first['id'])


class DefaultTeacher(ApplyCounter):
    def test_t3_3_rejection_closes_without_the_worker_recovery(self):
        self.base_style, self.candidate_style = 'gold', 'empty'
        with mock.patch.object(prequential, 'close_rejected', return_value=[]):
            self.golds('t', 99, empty=20)
            self.worker()
            self.golds('u', 5)
            self.worker()
            frozen = self.jobs()[-1]
            self.golds('v', 50, empty=9)
            self.gold('v-050', empty=True)
            result = self.worker()['cycle']
            self.assertEqual((result['status'], result['reason']), ('apply_blocked', 'apply_quality_regression'))
            self.assertEqual(self.evaluation(frozen['id']), ('blocked', 'apply_quality_regression'))
            self.worker()
            self.assertEqual(len(self.jobs()), 2)
            self.assertEqual(self.applied, [frozen['id']])


class HealFence(RenewalFixture):
    teacher_off_switch = False

    def test_t8_1_enable_intent_refuses_heal_and_leaves_live_selector(self):
        before = (self.root / 'live-selector.json').read_bytes()
        intent = self.root / learning_profile.TEACHER_ENABLE
        self.write(intent, {'schema': 'fixture-intent'})
        policy = json.loads(before)
        with self.assertRaises(selector_heal.HealRefused) as refused:
            selector_heal.check_scope(self.root, policy)
        self.assertEqual(str(refused.exception.args[0] if refused.exception.args else refused.exception),
                         'teacher_enable_pending')
        self.select()
        with self.assertRaises(selector_heal.HealRefused):
            selector_heal.rebind(self.root, canonicalize=True)
        self.assertEqual((self.root / 'live-selector.json').read_bytes(), before)
        intent.unlink()
        self.assertTrue(selector_heal.check_scope(self.root, policy))


if __name__ == '__main__':
    import unittest
    unittest.main()
