"""What the resident answered when it did not accept an application.

A ``worker_error`` is the resident failing while it checked or loaded the
candidate: one of the counted failures, tried again and ended at the limit.
Any other answer that is not an acceptance is its refusal of the candidate,
which ends the application at once. Both leave the status word in the journal
and in the result (``serviceStatus``); a word outside the fixed set is
``other``. The count of failures in a row goes on when another candidate
takes the turn, a failure that is no answer is recorded as a fixed word and
never as its text, and the answer is kept after the pointer is written and
across a stopped pass. Assertions are journal rows, result fields, event
counts and the bytes of the namespace's files.
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


class Reasons(unittest.TestCase):
    def test_every_reason_the_application_raises_is_a_listed_one(self):
        import re
        from pathlib import Path
        source = Path(A.__file__).read_text(encoding='utf8')
        raised = set(re.findall(r"(?:ValueError|ResourceDeferred)\('([a-z_]+)'\)", source))
        self.assertGreater(len(raised), 15)
        self.assertEqual(raised - A.REASONS, set())
        for text in ('fixture_private_note', 'apply_made_up', 'Expecting value: line 1', '', None, 7):
            self.assertEqual(A._code(ValueError(text)), A.OTHER_ERROR)
        self.assertEqual(A._code(KeyError('apply_quality_stale')), A.OTHER_ERROR)
        self.assertEqual(A._code(OSError('apply_quality_stale')), A.IO_ERROR)
        self.assertEqual(A._code(ValueError(A.STALE)), A.STALE)


class TwoCandidates(unittest.TestCase):
    """Two evaluated candidates of one namespace taking turns (review 11 F-18)."""

    fixture = table.ApplyTable.fixture
    later = table.ApplyTable.later

    def second(self, case):
        from memvane.learning.context_learning.learning_queue import LearningQueue
        cycle = LearningQueue(case.root).plan(case.train, approval=case.stage['authorizationSha256'],
                                              cohort='test', family='second-family', minimum=5, maximum=100)['id']
        folder = case.root / 'cycles' / cycle
        folder.mkdir(mode=0o700)
        case.write(folder / 'candidate.artifact', b'candidate')
        stage = {**case.stage, 'artifactPath': str(folder / 'candidate.artifact')}
        with case.db(None) as db:
            db.execute('INSERT INTO cycles VALUES(?,?,?,?,NULL)', (cycle, 'staged_evaluated', json.dumps(stage), 1.0))
        return cycle

    def test_the_failures_in_a_row_go_on_when_the_other_candidate_takes_the_turn(self):
        case = self.fixture()
        turns = [case.cycle, self.second(case)]
        calls, seen = [], []
        for index in range(A.FAILURE_LIMIT):
            with self.later(case):
                answer = A.apply_candidate(case.root, case.policy, turns[index % 2],
                                           transport=answering('worker_error', calls))
            seen.append((answer['status'], A.read_journal(case.root)['failures']))
        self.assertEqual(seen, [('apply_deferred', count) for count in range(1, A.FAILURE_LIMIT)]
                         + [('apply_blocked', A.FAILURE_LIMIT)])
        self.assertEqual(len(calls), A.FAILURE_LIMIT)
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['cycle_id'], row['serviceStatus']),
                         ('rejected', turns[(A.FAILURE_LIMIT - 1) % 2], 'worker_error'))
        # The closed journal ends that run: the other candidate starts its own count.
        with self.later(case):
            answer = A.apply_candidate(case.root, case.policy, turns[A.FAILURE_LIMIT % 2],
                                       transport=answering('worker_error', calls))
        self.assertEqual((answer['status'], answer['failures']), ('apply_deferred', 1))

    def test_the_backoff_of_one_candidates_failure_holds_for_the_other(self):
        case = self.fixture()
        other = self.second(case)
        calls = []
        first = A.apply_candidate(case.root, case.policy, case.cycle, transport=answering('worker_error', calls))
        self.assertEqual((first['status'], len(calls)), ('apply_deferred', 1))
        waiting = A.apply_candidate(case.root, case.policy, other, transport=answering('worker_error', calls))
        self.assertEqual((waiting['status'], waiting['next_retry'], len(calls)),
                         ('apply_deferred', first['next_retry'], 1))
        self.assertEqual(A.read_journal(case.root)['cycle_id'], case.cycle)


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

    # -- after the pointer is written (review 11 F-19) -----------------------------------
    def staged(self, fail):
        """A transport that accepts until ``fail`` (``second`` prepare or ``activate``) and then errs."""
        prepares = []

        def answer(command, record):
            if command == 'prepare':
                prepares.append(1)
                if fail == 'second' and len(prepares) > 1:
                    return {'status': 'worker_error'}
                return {'status': 'prepared', 'token': record['apply_id']}
            return {'status': 'worker_error'}
        return answer

    def pending(self):
        return [row for row in self.promotions() if row.get('outcome') == 'activation_pending']

    def profiled(self):
        """The profile file the pending row's project is read from."""
        from memvane.learning.context_learning import learning_profile
        self.write_json(self.root / learning_profile.NAME, {'proposal': self.profile})

    def errs_after_the_pointer(self, fail, reason):
        self.ready()
        self.profiled()
        self.transport = self.staged(fail)
        for count in range(1, A.FAILURE_LIMIT + 1):
            result = self.worker()['cycle']
            self.assertEqual((result['status'], result['reason'], result['failures'],
                              result['serviceStatus']), ('apply_blocked', reason, count, 'worker_error'))
            self.past_backoff()
        row = A.read_journal(self.root)
        self.assertEqual((row['phase'], row['serviceStatus']), ('pointer_committed', 'worker_error'))
        self.assertEqual([(event['reason'], event['serviceStatus']) for event in self.pending()],
                         [(reason, 'worker_error')])
        self.assertEqual(self.rejected(), [])

    def test_an_error_at_the_second_prepare_is_named_in_the_pending_row(self):
        self.errs_after_the_pointer('second', 'apply_generation_changed')

    def test_an_error_at_the_activation_is_named_in_the_pending_row(self):
        self.errs_after_the_pointer('activate', A.SERVICE_ERROR)

    # -- a failure that is no answer (review 11 F-20) --------------------------------------
    def texts(self):
        """Every event row and every file of the namespace, as bytes."""
        found = [json.dumps(self.recorded).encode()]
        for path in self.root.rglob('*'):
            if path.is_file():
                found.append(path.read_bytes())
        return b'\n'.join(found)

    def transport_error(self, fail, error=None, reason=A.IO_ERROR, secrets=('fixture-free-text-37', '/private/path')):
        self.ready()
        self.profiled()

        def answer(command, record):
            if command == fail:
                raise error or OSError('fixture-free-text-37 /private/path')
            return {'status': 'prepared', 'token': record['apply_id']}
        self.transport = answer
        reasons = []
        for _ in range(A.FAILURE_LIMIT):
            reasons.append(self.worker()['cycle']['reason'])
            self.past_backoff()
        self.assertEqual(set(reasons), {reason})
        self.assertEqual(A.read_journal(self.root)['reason'], reason)
        self.assertEqual(len(self.rejected() if fail == 'prepare' else self.pending()), 1)
        for secret in secrets:
            self.assertNotIn(secret.encode(), self.texts())

    def test_a_text_shaped_like_a_code_is_not_kept_either(self):
        # Review 12: only the listed reasons are kept, whatever an exception's text looks like.
        for fail in ('prepare', 'activate'):
            with self.subTest(fail=fail):
                self.assertNotIn('fixture_private_note', A.REASONS)
        self.transport_error('prepare', ValueError('fixture_private_note'), A.OTHER_ERROR,
                             ('fixture_private_note',))

    def test_a_text_shaped_like_a_code_at_the_activation_is_not_kept(self):
        self.transport_error('activate', ValueError('fixture_private_note'), A.OTHER_ERROR,
                             ('fixture_private_note',))

    def test_the_text_of_an_error_at_the_prepare_reaches_no_row_and_no_file(self):
        self.transport_error('prepare')

    def test_the_text_of_an_error_at_the_activation_reaches_no_row_and_no_file(self):
        self.transport_error('activate')

    # -- a pass stopped after the journal closed (review 11 F-21) ---------------------------
    def test_the_answer_is_named_when_a_later_pass_reports_the_closed_candidate(self):
        from unittest import mock
        from memvane.learning.context_learning import prequential

        class Stopped(BaseException):
            pass
        self.ready()
        self.transport = answering('worker_error')
        for _ in range(A.FAILURE_LIMIT - 1):
            self.assertEqual(self.worker()['cycle']['status'], 'apply_deferred')
            self.past_backoff()
        with mock.patch.object(prequential, 'record_pass', side_effect=Stopped):
            with self.assertRaises(Stopped):
                self.worker()
        self.assertEqual((A.read_journal(self.root)['phase'], self.rejected()), ('rejected', []))
        self.past_backoff()
        self.worker()
        closed = self.rejected()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['serviceStatus'], 'worker_error')


if __name__ == '__main__':
    unittest.main()
