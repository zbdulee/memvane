"""W13 r2 follow-ups F-8, F-9, F-10 and F-12 of the application.

The candidate-apply integration fixture (real database, journal, pointer and
evidence files; a fake resident transport) and the evaluation queue on a
private database. Every check counts attempts, failures and states.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memvane.learning.context_learning import candidate_apply as A
from memvane.learning.context_learning import candidate_close, prequential, runtime_authorization, shared
from memvane.learning.context_learning.evaluation_queue import EvaluationQueue
from memvane.learning.tests.integration import test_context_learning_candidate_apply as apply_tests


class Stopped(RuntimeError):
    pass


class Case(unittest.TestCase):
    def fixture(self):
        case = apply_tests.Checks('test_success_and_repeated_wake_never_republishes_or_trains')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def stop_at(self, case, point):
        def stop(at):
            if at == point:
                raise Stopped()
        with self.assertRaises(Stopped):
            case.run_apply(fault=stop)

    def later(self, case):
        path = case.root / A.JOURNAL
        retry = A.read_journal(case.root).get('retry_at', 0) if path.exists() else 0
        return patch.object(A.time, 'time', return_value=max(retry, time.time()) + 1)

    def phase(self, case):
        return A.read_journal(case.root)['phase']

    # What can change between the pointer and the activation without anyone deciding anything.
    def edit_member_card(self, case):
        gone = case.train[0][1]
        patch.object(shared, '_case_active', side_effect=lambda _root, key: key[1] != gone).start()

    def renew_approval(self, case):
        case.grant['binding'] = {'pluginVersion': 'next'}
        self.assertNotEqual(runtime_authorization.proposal_sha256(case.grant), case.stage['authorizationSha256'])

    def new_gold(self, case):
        case.validation.append(case.row(900, 'val'))

    def drop_member_row(self, case):
        del case.train[0]

    CHANGES = ('edit_member_card', 'renew_approval', 'new_gold', 'drop_member_row')


class AfterThePointer(Case):
    """F-8: once the pointer is the candidate's, only the journal's identity is checked again."""

    def test_a_change_of_the_sources_or_the_approval_no_longer_strands_the_activation(self):
        for change in self.CHANGES:
            with self.subTest(change=change):
                case = self.fixture()
                self.stop_at(case, 'pointer_committed')
                getattr(self, change)(case)
                with self.later(case):
                    answer = case.run_apply()
                self.assertEqual((answer['status'], self.phase(case)), ('applied', 'activated'), answer)
                A.readable(case.root)
                patch.stopall()

    def test_the_same_change_before_the_pointer_still_keeps_the_candidate_out(self):
        for change in self.CHANGES:
            with self.subTest(change=change):
                case = self.fixture()
                self.stop_at(case, 'prepared')
                getattr(self, change)(case)
                with self.later(case):
                    answer = case.run_apply()
                self.assertNotEqual(answer['status'], 'applied', answer)
                self.assertEqual(json.loads((case.root / 'active-checkpoint.json').read_text()), case.old)
                patch.stopall()

    def test_a_changed_artifact_or_stage_is_still_refused_after_the_pointer(self):
        case = self.fixture()
        self.stop_at(case, 'pointer_committed')
        artifact = case.root / 'cycles' / case.cycle / 'candidate.artifact'
        case.write(artifact, b'another')
        with self.later(case):
            answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason'], answer['failures'], self.phase(case)),
                         ('apply_blocked', 'apply_source_or_artifact_changed', 1, 'pointer_committed'))
        case.write(artifact, b'candidate')
        with case.db(None) as db:
            db.execute('UPDATE cycles SET evidence=?', (shared.canonical({**case.stage, 'note': 'changed'}),))
        with self.later(case):
            answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', 'apply_intent_changed'))

    def test_a_stuck_activation_is_logged_once_at_the_limit(self):
        case = self.fixture()
        rows = []
        with patch.object(prequential, '_emit', side_effect=lambda project, event, **fields: rows.append((event, fields))), \
                patch.object(candidate_close, '_project_of', return_value=str(case.project), create=True):
            for failures in range(1, A.FAILURE_LIMIT + 3):
                answer = {'status': 'apply_blocked', 'reason': 'apply_generation_changed', 'cycle_id': case.cycle,
                          'failures': failures, 'activationPending': failures >= A.FAILURE_LIMIT}
                candidate_close.unapplied(case.root, case.cycle, answer, None, None)
        self.assertEqual(rows, [('learning.promotion', {'outcome': 'activation_pending',
                                                        'reason': 'apply_generation_changed', 'cycle': case.cycle})])


class FailuresInARow(Case):
    """F-9: a failure that recurs between the resident's answer and the pointer reaches the limit."""

    def test_five_failures_after_prepare_end_the_application(self):
        case = self.fixture()

        def broken(at):
            if at == 'prepared':
                raise ValueError('apply_inference_evidence_changed')
        seen = []
        for _ in range(A.FAILURE_LIMIT):
            with self.later(case):
                answer = case.run_apply(fault=broken)
            seen.append((answer['status'], answer.get('failures')))
        self.assertEqual(seen, [('apply_deferred', count) for count in range(1, A.FAILURE_LIMIT)]
                         + [('apply_blocked', None)])
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['reason'], row['failures']),
                         ('rejected', 'apply_inference_evidence_changed', A.FAILURE_LIMIT))
        self.assertEqual(case.events.count('prepare'), A.FAILURE_LIMIT)
        self.assertEqual(json.loads((case.root / 'active-checkpoint.json').read_text()), case.old)

    def test_the_count_starts_again_once_the_pointer_is_committed(self):
        case = self.fixture()

        def broken(at):
            if at == 'prepared':
                raise ValueError('apply_inference_evidence_changed')
        with self.later(case):
            case.run_apply(fault=broken)
        self.assertEqual(A.read_journal(case.root)['failures'], 1)
        with self.later(case):
            self.assertEqual(case.run_apply()['status'], 'applied')
        self.assertEqual(A.read_journal(case.root)['failures'], 0)


class EvaluationCount(unittest.TestCase):
    """F-10: failed applications and failed evaluations do not add up on one evaluation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        root.chmod(0o700)
        self.q = EvaluationQueue(root)
        self.job = self.q.ensure('cohort-' + 'a' * 20, 'stage', 'validation')['id']

    def state(self):
        row = self.q.get(self.job)
        return row['status'], row['failures'], row['reason']

    def test_failed_applications_do_not_close_a_later_evaluation_that_is_only_deferred(self):
        self.q.finish(self.job, 'completed')
        for _ in range(3):
            self.q.apply_failed(self.job, 'apply_prepared_inference_required', A.FAILURE_LIMIT)
        self.assertEqual(self.state(), ('completed', 3, None))
        self.q.refresh('cohort-' + 'a' * 20)
        self.assertEqual(self.state(), ('queued', 0, None))
        self.q.defer(self.job, 'resource')
        self.assertEqual(self.state(), ('deferred', 0, 'resource'))

    def test_failed_evaluation_attempts_do_not_shorten_the_limit_of_applications(self):
        for _ in range(2):
            self.q.defer(self.job, 'evaluation_failed', failure=True)
        self.assertEqual(self.state()[:2], ('deferred', 2))
        with self.q.claim(self.job, now=time.time() + 10000) as lease:
            self.assertIsNotNone(lease)
            self.q.finish(self.job, 'completed')
        self.assertEqual(self.state(), ('completed', 0, None))
        for count in range(1, A.FAILURE_LIMIT):
            self.q.apply_failed(self.job, 'apply_prepared_inference_required', A.FAILURE_LIMIT)
            self.assertEqual(self.state(), ('completed', count, None))
        self.q.apply_failed(self.job, 'apply_prepared_inference_required', A.FAILURE_LIMIT)
        self.assertEqual(self.state(), ('blocked', A.FAILURE_LIMIT, 'apply_prepared_inference_required'))

    def test_an_evaluation_that_fails_three_times_still_closes_with_its_reason(self):
        for _ in range(3):
            self.q.defer(self.job, 'evaluation_failed', failure=True)
        self.assertEqual(self.state(), ('blocked', 3, 'evaluation_failed'))
        # A waiting evaluation keeps its count: only a completed or refreshed one starts again.
        other = EvaluationQueue(self.q.root)
        job = other.ensure('cohort-' + 'b' * 20, 'stage', 'validation')['id']
        other.defer(job, 'evaluation_failed', failure=True)
        with other.claim(job, now=time.time() + 10000):
            other.finish(job, 'waiting_quality')
        self.assertEqual(other.get(job)['failures'], 1)


class StaleAtTheResident(Case):
    """F-12: an evaluation the resident finds stale is evaluated again, not rejected."""

    def test_the_resident_answers_with_its_own_code(self):
        from memvane.learning.context_learning import inference_service as service
        self.assertEqual(service._failure(service.ServiceFailure(A.STALE)), A.STALE)
        self.assertEqual(service._failure(ValueError('apply_quality_regression')), 'worker_error')

    def test_the_resident_names_a_stale_evaluation_and_nothing_else(self):
        from memvane.learning.context_learning import inference_service as service
        case = self.fixture()
        self.stop_at(case, 'intent')
        row = A.read_journal(case.root)
        envelope = {'command': 'apply_prepare', 'schema': service.PROTOCOL, 'signature': service._signature(),
                    'state_dir': str(case.root), 'namespace': service.namespace(case.root), 'apply_id': row['apply_id'],
                    'queued_at_epoch': time.time(), 'deadline_epoch': time.time() + 20}
        for reason, code in ((A.STALE, A.STALE), ('apply_quality_regression', 'worker_error')):
            with patch.object(shared, '_policy', return_value=case.policy), \
                    patch.object(A, 'validate_candidate', side_effect=ValueError(reason)):
                with self.assertRaises(Exception) as caught:
                    service._validate_apply(envelope)
            self.assertEqual(service._failure(caught.exception), code)

    def test_the_worker_decides_nothing_on_that_answer(self):
        case = self.fixture()
        answers = iter([{'status': A.STALE}])
        calls = []

        def transport(command, record):
            calls.append(command)
            return next(answers)
        answer = A.apply_candidate(case.root, case.policy, case.cycle, transport=transport)
        self.assertEqual((answer['status'], answer['reason'], calls), ('apply_blocked', A.STALE, ['prepare']))
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row.get('failures', 0)), ('intent', 0))
        self.assertEqual(json.loads((case.root / 'active-checkpoint.json').read_text()), case.old)
        # The next attempt, after the evaluation is current again, applies.
        with self.later(case):
            self.assertEqual(case.run_apply()['status'], 'applied')
