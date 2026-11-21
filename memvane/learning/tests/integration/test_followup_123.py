"""1.0.23 review follow-ups F-1, F-2, F-7 and F-8 (learning).

F-1 and F-2 run on the real renewal install through the worker command
(``test_carry_122`` fixture). F-7 and F-8 use the fake host CLIs
(``test_teacher_parked_122`` fixture). Assertions are rows, ledger stages,
call counts and breaker counters, never elapsed time.
"""
from __future__ import annotations

import errno
import json
from unittest import mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import (candidate_close, general_teacher, shared,
                                               teacher_consent)
from memvane.learning.context_learning.evaluation_queue import EvaluationQueue
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture
from memvane.learning.tests.integration.test_carry_122 import CarryFixture
from memvane.learning.tests.integration.test_teacher_parked_122 import ParkMixin
from memvane.learning.tests.integration.test_worker_teacher_121 import WorkerFixture
from tests.support.case import FixtureCase


class RunningEvaluation(CarryFixture):
    """F-1: an evaluation left ``running`` by a stopped worker closes with its approval."""

    def test_f1_running_evaluation_of_an_earlier_approval_is_closed(self):
        self.golds('g', 5, empty=1)
        cycle = self.legacy_job(5)
        queue = EvaluationQueue(self.root)
        queue.ensure(cycle, 'stage', 'validation')
        with shared._db(self.root) as db:
            db.execute("UPDATE learning_evaluations SET status='running',attempts=1 WHERE cycle_id=?", (cycle,))
        self.assertEqual(self.evaluations()[cycle][0], 'running')
        self.renew()
        self.golds('h', 5)
        due = self.due.call_count
        result = self.worker()
        self.assertEqual(self.evaluations()[cycle][:2], ('blocked', 'authorization_superseded'))
        self.assertEqual(self.due.call_count, due + 1, result)
        self.assertEqual(len(self.promotions('superseded')), 1)
        self.worker()
        self.assertEqual(self.due.call_count, due + 2)
        self.assertEqual(len(self.promotions('superseded')), 1)

    def test_f1_reject_moves_a_running_row_and_a_claim_does_not_reopen_it(self):
        queue = EvaluationQueue(self.root)
        queue.ensure('cycle-x', 'stage', 'validation')
        with queue.claim('evaluate:cycle-x') as lease:
            self.assertIsNotNone(lease)
            self.assertEqual(queue.get('evaluate:cycle-x')['status'], 'running')
            self.assertEqual(queue.reject('cycle-x', 'authorization_superseded')['status'], 'blocked')
        row = queue.get('evaluate:cycle-x')
        self.assertEqual((row['status'], row['reason']), ('blocked', 'authorization_superseded'))
        self.assertEqual(queue.pending(), [])


class DeferredCarry(CarryFixture):
    """F-2: momentary pressure while carrying closes nothing."""

    def test_f2_resource_deferral_keeps_the_candidate_and_the_next_pass_carries(self):
        job = self.frozen()
        _old, new = self.renew()
        before = (self.jobs(), self.cycle_rows(), self.evaluations())
        due = self.due.call_count
        with mock.patch.object(candidate_close, 'carry_candidate',
                               side_effect=ResourceDeferred('fixture_pressure')) as carry:
            result = self.worker()
        self.assertEqual(carry.call_count, 1)
        self.assertEqual(result['cycle'], {'status': 'deferred_resource', 'reason': 'fixture_pressure'})
        self.assertEqual((self.jobs(), self.cycle_rows(), self.evaluations()), before)
        self.assertEqual(self.due.call_count, due)
        self.assertEqual(self.promotions('superseded') + self.promotions('carried'), [])
        result = self.worker()['cycle']
        self.assertEqual(result['cycle_id'], job['id'])
        self.assertEqual(self.jobs()[-1]['_approval'], new)
        self.assertEqual(len(self.promotions('carried')), 1)
        self.assertEqual(self.promotions('superseded'), [])
        self.assertEqual(self.trained, [100])

    def test_f2_another_refusal_still_closes(self):
        job = self.frozen()
        self.renew()
        with mock.patch.object(candidate_close, 'carry_candidate',
                               side_effect=ValueError('carry_candidate_changed')):
            self.worker()
        self.assertEqual(self.evaluations()[job['id']][:2], ('blocked', 'authorization_superseded'))


class PreSendPressure(ParkMixin, TeacherFixture):
    def setUp(self):
        super().setUp()
        self.install_scope()

    def test_f7_system_pressure_before_the_body_spends_no_attempt(self):
        self.add('a1')
        # Ready at the pass's own check; not ready once the child runs and no body left yet.
        report = lambda *a, **k: {'ready': self.stage('a1') != 'dispatched' or bool(self.bodies())}
        with mock.patch.object(general_teacher, 'resource_report', side_effect=report):
            for _ in range(6):
                self.assertEqual(self.run_pass()['status'], 'deferred_resource')
        self.assertEqual(self.bodies(), 0)
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('queued', 'pre_send_rejected'))
        self.assertEqual((self.retries(), self.breaker()), (0, 0))
        self.assertEqual(self.teacher_events('pending_review'), [])
        rejected = self.teacher_events('pre_send_rejected')
        self.assertEqual([row['reason'] for row in rejected], ['gold_system_pressure'] * 6)
        self.run_pass()
        self.assertEqual((self.cases()['a1'][0], self.bodies()), ('gold', 1))

    def test_f8_scope_and_consent_refusals_after_the_answer_spare_the_breaker(self):
        codes = ('project_scope_binding_changed', 'teacher_consent_scope', 'gold_other_failure')
        for index, code in enumerate(codes):
            with self.subTest(code=code):
                with shared._db(self.root) as db:
                    ledger.ensure(db)
                    db.execute('DELETE FROM cases')
                    db.execute('DELETE FROM project_gold_attempts')
                self.add('a1', tag=str(index))
                self.fail_at = {'answer_received': code}
                self.run_pass()
                self.fail_at = {}
                self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'gold_attempt_failed'))
                self.assertEqual(self.breaker(), 1 if code == 'gold_other_failure' else 0)

    def test_f8_confirmed_kept_answer_leaves_the_choice_in_first(self):
        self.park()
        kept = json.loads(self.cases()['a1'][3])
        self.run_pass()
        row = self.cases()['a1']
        self.assertEqual(row[0], 'gold')
        self.assertEqual(row[3], canonical(kept['choice']))


class UnreadableConsent(ParkMixin, TeacherFixture):
    """F-8: a consent read that could not finish is not a withdrawal."""

    def setUp(self):
        super().setUp()
        self.install_scope()

    def unreadable(self):
        return mock.patch.object(general_teacher, '_consent_file_v2',
                                 side_effect=ResourceDeferred(teacher_consent.UNREADABLE))

    def test_f8_kept_answer_survives_an_unreadable_consent(self):
        self.park()
        with self.unreadable():
            for _ in range(3):
                result = self.run_pass()
                self.assertEqual((result['status'], result['reason']),
                                 ('deferred_resource', teacher_consent.UNREADABLE))
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('first_done', 'answer_parked'))
        self.assertEqual(self.teacher_events('pending_review'), [])
        self.run_pass()
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('gold', 'committed'))
        self.assertEqual(self.runs('claude'), 1)

    def test_f8_withdrawn_consent_still_ends_the_kept_answer(self):
        self.park()
        with mock.patch.object(general_teacher, '_consent_file_v2',
                               side_effect=ValueError('teacher_consent_required')):
            self.assertEqual(self.run_pass()['status'], 'consent_required')
        self.assertEqual(self.cases()['a1'][:2], ('pending_review', 'teacher_consent_required'))
        self.assertEqual(self.stage('a1'), 'dispatched_uncertain')

    def test_f8_unreadable_consent_after_the_answer_keeps_it(self):
        self.add('a1')
        proposal = dict(self.proposal)

        def consent(root, project):
            if self.stage('a1') == 'answer_received':
                raise ResourceDeferred(teacher_consent.UNREADABLE)
            return dict(proposal)
        with mock.patch.object(general_teacher, '_consent_file_v2', side_effect=consent):
            self.assertEqual(self.run_pass()['status'], 'deferred_resource')
        self.assertEqual((self.cases()['a1'][0], self.stage('a1'), self.breaker()),
                         ('first_done', 'answer_parked', 0))
        self.run_pass()
        self.assertEqual((self.cases()['a1'][0], self.runs('claude')), ('gold', 1))


class UnreadableConsentWorker(ParkMixin, WorkerFixture):
    def setUp(self):
        super().setUp()
        self.install_scope()

    def test_f8_worker_defers_and_keeps_the_answer(self):
        self.add('a1')
        self.defer_at = ('answer_received',)
        self.worker()
        self.defer_at = ()
        self.assertEqual(self.cases()['a1'][0], 'first_done')
        with mock.patch.object(general_teacher, '_consent_file_v2',
                               side_effect=ResourceDeferred(teacher_consent.UNREADABLE)):
            result = self.worker()
        self.assertEqual((result['status'], result['teacher']['reason']),
                         ('deferred_resource', teacher_consent.UNREADABLE))
        self.assertEqual((self.cases()['a1'][0], self.stage('a1')), ('first_done', 'answer_parked'))
        self.worker()
        self.assertEqual((self.cases()['a1'][0], self.runs('claude')), ('gold', 1))


class ConsentFileErrors(FixtureCase):
    """The file check itself: which I/O errors describe the file and which defer."""

    def setUp(self):
        super().setUp()
        self.root = self.home / 'ns'
        self.root.mkdir(mode=0o700)
        self.project = (self.home / 'project')
        self.project.mkdir()
        self.project = self.project.resolve()
        value = teacher_consent.proposal(self.project, granted_by='default-enrollment',
                                         granted_at_unix_ms=1)
        path = self.root / teacher_consent.NAME
        path.write_text(canonical(teacher_consent.envelope(value)))
        path.chmod(0o600)
        self.value = value

    def read(self, error=None):
        if error is None:
            return teacher_consent.require_consent_v2(self.root, self.project)
        with mock.patch.object(teacher_consent, 'read_private_json', side_effect=error):
            return teacher_consent.require_consent_v2(self.root, self.project)

    def test_readable_file_is_returned(self):
        self.assertEqual(self.read(), self.value)

    def test_io_errors_defer_and_file_errors_refuse(self):
        for code in (errno.EIO, errno.EMFILE, errno.ENFILE, errno.EINTR, errno.ETIMEDOUT):
            with self.subTest(errno=code):
                with self.assertRaises(ResourceDeferred) as raised:
                    self.read(OSError(code, 'synthetic'))
                self.assertEqual(str(raised.exception), teacher_consent.UNREADABLE)
        for code in (errno.ELOOP, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.EISDIR):
            with self.subTest(errno=code):
                with self.assertRaises(ValueError) as raised:
                    self.read(OSError(code, 'synthetic'))
                self.assertNotIsInstance(raised.exception, ResourceDeferred)
                self.assertEqual(str(raised.exception), 'teacher_consent_scope')
        with self.assertRaises(ValueError) as raised:
            self.read(OSError(errno.ENOENT, 'synthetic'))
        self.assertEqual(str(raised.exception), 'teacher_consent_required')
        with self.assertRaises(ValueError) as raised:
            self.read(ValueError('unsafe_input_file'))
        self.assertEqual(str(raised.exception), 'teacher_consent_scope')
