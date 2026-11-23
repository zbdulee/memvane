"""What the resident answered when it did not accept an application.

A ``worker_error`` is the resident failing while it checked or loaded the
candidate: one of the counted failures, tried again and ended at the limit.
Any other answer that is not an acceptance is its refusal of the candidate,
which ends the application at once. Both leave the status word in the journal
and in the result (``serviceStatus``); a word outside the fixed set is
``other``. Assertions are journal rows, result fields and event counts.
"""
from __future__ import annotations

import json
import unittest

from memvane.learning.context_learning import candidate_apply as A
from memvane.learning.context_learning import shared
from memvane.learning.tests.integration import test_worker_teacher_121 as workers
from memvane.learning.tests.statemachine import test_m06_apply as table


def answering(status, calls=None):
    def answer(command, record):
        if calls is not None:
            calls.append(command)
        return {'status': status, 'prediction': None, 'timing': {}}
    return answer


class ServiceAnswer(unittest.TestCase):
    fixture = table.ApplyTable.fixture
    later = table.ApplyTable.later
    phase = table.ApplyTable.phase

    def attempt(self, case, transport):
        with self.later(case):
            return A.apply_candidate(case.root, case.policy, case.cycle, transport=transport)

    def test_a_worker_error_is_tried_again_and_ends_at_the_limit(self):
        case = self.fixture()
        for count in range(1, A.FAILURE_LIMIT):
            answer = self.attempt(case, answering('worker_error'))
            self.assertEqual((answer['status'], answer['reason'], answer['failures'], answer['serviceStatus']),
                             ('apply_deferred', A.SERVICE_ERROR, count, 'worker_error'))
            row = A.read_journal(case.root)
            self.assertEqual((row['phase'], row['failures'], row['serviceStatus']), ('intent', count, 'worker_error'))
        answer = self.attempt(case, answering('worker_error'))
        self.assertEqual((answer['status'], answer['reason'], answer['serviceStatus']),
                         ('apply_blocked', A.SERVICE_ERROR, 'worker_error'))
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['reason'], row['failures'], row['serviceStatus']),
                         ('rejected', A.SERVICE_ERROR, A.FAILURE_LIMIT, 'worker_error'))
        self.assertEqual(json.loads((case.root / 'active-checkpoint.json').read_text()), case.old)
        self.assertNotIn(A.SERVICE_ERROR, A.REFUSED)

    def test_a_worker_error_that_stops_recurring_applies(self):
        case = self.fixture()
        for _ in range(A.FAILURE_LIMIT - 1):
            self.assertEqual(self.attempt(case, answering('worker_error'))['status'], 'apply_deferred')
        with self.later(case):
            answer = case.run_apply()
        self.assertEqual(answer['status'], 'applied')
        self.assertNotIn('serviceStatus', A.read_journal(case.root))

    def test_a_refusal_ends_at_once_and_names_the_answer(self):
        for status, recorded in (('model_family_mismatch', 'model_family_mismatch'),
                                 ('request_changed', 'request_changed'),
                                 ('failed', 'other'), ('/private/path in a message', 'other'), (None, 'other')):
            with self.subTest(status=status):
                case = self.fixture()
                answer = A.apply_candidate(case.root, case.policy, case.cycle, transport=answering(status))
                self.assertEqual((answer['status'], answer['reason'], answer['serviceStatus']),
                                 ('apply_blocked', 'apply_prepare_failed', recorded))
                row = A.read_journal(case.root)
                self.assertEqual((row['phase'], row['reason'], row['serviceStatus']),
                                 ('rejected', 'apply_prepare_failed', recorded))

    def test_a_failure_that_is_no_answer_carries_no_status(self):
        case = self.fixture()

        def broken(command, record):
            raise OSError('fixture socket')
        answer = self.attempt(case, answering('worker_error'))
        self.assertEqual(answer['serviceStatus'], 'worker_error')
        answer = self.attempt(case, broken)
        self.assertEqual((answer['status'], answer['failures']), ('apply_deferred', 2))
        self.assertNotIn('serviceStatus', answer)
        self.assertNotIn('serviceStatus', A.read_journal(case.root))


class WorkerPasses(workers.WorkerFixture):
    """Through the worker (default profile): how often the candidate is reported closed."""

    freeze = workers.Learning.freeze

    def ready(self):
        self.freeze()
        self.golds('v', 50, empty=10)

    def past_backoff(self):
        """Past every backoff: the journal's and the evaluation's."""
        path = self.root / A.JOURNAL
        if path.exists():
            row = A.read_journal(self.root)
            if row.get('retry_at'):
                row['retry_at'] = 0
                A.put(path, row)
        with shared._db(self.root) as db:
            db.execute("UPDATE learning_evaluations SET retry_at=0 WHERE status IN ('queued','deferred')")

    def rejected(self):
        return [row for row in self.promotions() if row.get('outcome') == 'rejected']

    def test_five_worker_errors_close_the_candidate_once(self):
        self.ready()
        calls = []
        self.transport = answering('worker_error', calls)
        statuses = []
        for _ in range(A.FAILURE_LIMIT):
            self.assertEqual(self.rejected(), [])
            statuses.append(self.worker()['cycle']['status'])
            self.past_backoff()
        self.assertEqual(statuses, ['apply_deferred'] * (A.FAILURE_LIMIT - 1) + ['apply_blocked'])
        closed = self.rejected()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['serviceStatus'], 'worker_error')
        self.assertEqual(A.read_journal(self.root)['phase'], 'rejected')
        # Closed: later passes neither apply it again nor report it again.
        self.assertEqual(calls, ['prepare'] * A.FAILURE_LIMIT)
        for _ in range(2):
            self.past_backoff()
            self.worker()
        self.assertEqual((len(calls), len(self.rejected())), (A.FAILURE_LIMIT, 1))

    def test_a_refusal_closes_the_candidate_on_the_first_pass(self):
        self.ready()
        self.transport = answering('model_family_mismatch')
        first = self.worker()['cycle']
        self.assertEqual((first['status'], first['reason']), ('apply_blocked', 'apply_prepare_failed'))
        closed = self.rejected()
        self.assertEqual(len(closed), 1, self.promotions())
        self.assertEqual(closed[0]['serviceStatus'], 'model_family_mismatch')


if __name__ == '__main__':
    unittest.main()
