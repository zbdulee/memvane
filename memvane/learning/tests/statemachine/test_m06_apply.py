"""M6 (application) and M7 (active checkpoint): every phase under every kind of attempt.

The fixture is the candidate-apply integration fixture (real database,
journal, pointer and evidence files; a fake resident transport). A phase is
built by stopping a real attempt at its fault point. A stimulus is one
attempt, or five, of a kind; it names the model events it tries in order and
the expected phase is the model's ``next`` folded over them. The pointer is
the candidate's exactly from ``pointer_committed`` on (M7).
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from memvane.learning.context_learning import candidate_apply as A
from memvane.learning.context_learning.state_model import ABSENT, M6, M7
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.integration import test_context_learning_candidate_apply as apply_tests

_WHOLE = ['intent_formed', 'prepared_by_service', 'pointer_committed', 'activated_by_service']
# stimulus -> the model events it attempts, in order.
STIMULI = {
    'attempt': _WHOLE,
    'resident_refuses': ['intent_formed', 'ended'],
    # The candidate is judged in the check between the resident's answer and the pointer.
    'judged_after_prepare': ['intent_formed', 'prepared_by_service', 'ended'],
    'one_other_failure': ['intent_formed'],
    'five_other_failures': ['intent_formed', 'ended'],
    'momentary_resource_state': ['intent_formed'],
    # Before the pointer is written a stale evaluation decides nothing; after it the
    # decision is already made and the application is finished.
    'stale_evaluation': ['activated_by_service'],
    # The owner's maintenance command: only a written candidate pointer is put back.
    'owner_rollback': ['rolled_back'],
}
STOP_AT = {'intent': 'intent', 'prepared': 'prepared', 'pointer_committed': 'pointer_committed'}


class Stopped(RuntimeError):
    pass


def fold(state, events):
    for event in events:
        state = M6.next(state, event)
    return state


class ApplyTable(unittest.TestCase):
    def fixture(self):
        case = apply_tests.Checks('test_success_and_repeated_wake_never_republishes_or_trains')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def build(self, state):
        case = self.fixture()
        if state in STOP_AT:
            def stop(at):
                if at == STOP_AT[state]:
                    raise Stopped()
            with self.assertRaises(Stopped):
                case.run_apply(fault=stop)
        elif state == 'activated':
            self.assertEqual(case.run_apply()['status'], 'applied')
        elif state == 'rejected':
            self.refuse(case)
        return case

    def phase(self, case):
        path = case.root / A.JOURNAL
        return A.read_journal(case.root)['phase'] if path.exists() else ABSENT

    def pointer(self, case):
        current = json.loads((case.root / 'active-checkpoint.json').read_text())
        return 'incumbent' if current == case.old else 'candidate'

    def later(self, case):
        """A clock past any backoff the journal holds."""
        path = case.root / A.JOURNAL
        retry = A.read_journal(case.root).get('retry_at', 0) if path.exists() else 0
        return patch.object(A.time, 'time', return_value=max(retry, A.time.time()) + 1)

    def refuse(self, case):
        failed = lambda command, record: {'status': 'failed'}
        return A.apply_candidate(case.root, case.policy, case.cycle, transport=failed)

    def other_failure(self, case):
        def broken(command, record):
            raise OSError('fixture socket')
        with self.later(case):
            return A.apply_candidate(case.root, case.policy, case.cycle, transport=broken)

    def stimulate(self, case, name):
        if name == 'attempt':
            with self.later(case):
                return case.run_apply()
        if name == 'resident_refuses':
            with self.later(case):
                return self.refuse(case)
        if name == 'judged_after_prepare':
            def judge(at):
                if at == 'prepared':
                    raise ValueError('apply_quality_regression')
            with self.later(case):
                return case.run_apply(fault=judge)
        if name == 'one_other_failure':
            return self.other_failure(case)
        if name == 'five_other_failures':
            for _ in range(A.FAILURE_LIMIT):
                answer = self.other_failure(case)
            return answer
        if name == 'momentary_resource_state':
            case.low = True
            with self.later(case):
                return case.run_apply()
        if name == 'stale_evaluation':
            # A gold case confirmed since the evaluation: its cases are no longer the current ones.
            case.validation.append(case.row(900, 'val'))
            try:
                with self.later(case):
                    return case.run_apply()
            finally:
                case.validation.pop()
        if name == 'owner_rollback':
            from memvane.learning.context_learning import candidate_rollback
            return candidate_rollback.restore(case.root)
        raise AssertionError(name)

    def test_every_phase_under_every_attempt_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M6.states:
            for name, events in STIMULI.items():
                with self.subTest(state=state, stimulus=name):
                    case = self.build(state)
                    self.assertEqual(self.phase(case), state)
                    answer = self.stimulate(case, name)
                    expected = fold(state, events)
                    if state == 'pointer_committed' and name == 'judged_after_prepare':
                        # The check before the pointer is behind it: the attempt finishes.
                        expected = 'activated'
                    elif state == 'pointer_committed' and name not in ('attempt', 'stale_evaluation',
                                                                       'owner_rollback'):
                        # The pointer is already the candidate's: a failed attempt cannot end
                        # the application, and no later attempt forms the intent again.
                        expected = state
                    elif name == 'stale_evaluation' and state == 'prepared':
                        # Stale before the pointer: the journal is left for the re-evaluation.
                        expected = state
                    self.assertEqual(self.phase(case), expected, answer)
                    # M7 / G-7: the pointer changed exactly when the phase passed ``prepared``.
                    moved = expected in ('pointer_committed', 'activated')
                    self.assertEqual(self.pointer(case), M7.next('incumbent', 'pointer_written') if moved
                                     else 'incumbent')
                    if expected == 'activated':
                        A.readable(case.root)
                cells += 1
                at = state
                for event in events:
                    if (at, event) in M6.transitions:
                        driven.add((at, event))
                    at = M6.next(at, event)
        self.assertEqual(cells, len(M6.states) * len(STIMULI))
        self.assertEqual((len(M6.states), len(STIMULI)), (6, 8))
        self.assertEqual(driven, set(M6.transitions))
        self.assertEqual({event for events in STIMULI.values() for event in events}, set(M6.events))


class ApplyFailures(unittest.TestCase):
    fixture = ApplyTable.fixture
    later = ApplyTable.later
    other_failure = ApplyTable.other_failure
    phase = ApplyTable.phase

    def metrics(self, case, **candidate):
        stage = dict(case.stage, metrics=dict(case.stage['metrics'],
                                              candidate=dict(case.stage['metrics']['candidate'], **candidate)))
        with case.db(None) as conn:
            conn.execute('UPDATE cycles SET evidence=?', (canonical(stage),))
        case.stage = stage

    def test_a_candidate_is_applied_only_when_exact_improves(self):
        case = self.fixture()
        incumbent = case.stage['metrics']['incumbent']['exact_rate']
        self.metrics(case, exact_rate=incumbent)
        answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', 'apply_quality_not_improved'))
        self.assertIn('apply_quality_not_improved', A.JUDGED)
        self.assertEqual((self.phase(case), case.events), (ABSENT, []))
        # Better than the incumbent and below the policy file's old absolute target.
        case.policy['targetExact'] = .99
        self.metrics(case, exact_rate=incumbent + .01)
        self.assertLess(incumbent + .01, case.policy['targetExact'])
        self.assertEqual(case.run_apply()['status'], 'applied')

    def test_other_failures_are_tried_again_with_a_backoff_and_end_at_the_limit(self):
        case = self.fixture()
        waits = []
        for count in range(1, A.FAILURE_LIMIT):
            answer = self.other_failure(case)
            self.assertEqual((answer['status'], answer['reason'], answer['failures']),
                             ('apply_deferred', A.IO_ERROR, count))
            row = A.read_journal(case.root)
            self.assertEqual((row['phase'], row['failures']), ('intent', count))
            waits.append(row['retry_at'])
            # Before its time the attempt is not made at all.
            before = list(case.events)
            self.assertEqual(case.run_apply()['status'], 'apply_deferred')
            self.assertEqual(case.events, before)
        self.assertEqual(waits, sorted(waits))
        answer = self.other_failure(case)
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', A.IO_ERROR))
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['reason'], row['failures']),
                         ('rejected', A.IO_ERROR, A.FAILURE_LIMIT))
        self.assertEqual(json.loads((case.root / 'active-checkpoint.json').read_text()), case.old)

    def test_an_attempt_that_works_after_failures_applies(self):
        case = self.fixture()
        for _ in range(A.FAILURE_LIMIT - 1):
            self.other_failure(case)
        with self.later(case):
            self.assertEqual(case.run_apply()['status'], 'applied')

    def test_a_stale_evaluation_under_an_intent_decides_nothing_and_the_fresh_one_applies(self):
        case = self.fixture()

        def stop(at):
            if at == 'intent':
                raise Stopped()
        with self.assertRaises(Stopped):
            case.run_apply(fault=stop)
        first = A.read_journal(case.root)
        fresh = case.stage['metrics']['evaluatedAt']
        self.metrics(case)  # same values
        stale = dict(case.stage, metrics=dict(case.stage['metrics'], evaluatedAt=1))
        with case.db(None) as conn:
            conn.execute('UPDATE cycles SET evidence=?', (canonical(stale),))
        answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', A.STALE))
        self.assertEqual(A.read_journal(case.root), first)
        # Evaluated again: the stage evidence is new, and the intent is formed from it.
        again = dict(case.stage, metrics=dict(case.stage['metrics'], evaluatedAt=fresh - 1))
        with case.db(None) as conn:
            conn.execute('UPDATE cycles SET evidence=?', (canonical(again),))
        self.assertEqual(case.run_apply()['status'], 'applied')
        self.assertNotEqual(A.read_journal(case.root)['stage_sha256'], first['stage_sha256'])

    def stop_at(self, case, point):
        def stop(at):
            if at == point:
                raise Stopped()
        with self.assertRaises(Stopped):
            case.run_apply(fault=stop)

    def new_evidence(self, case):
        """What a teacher pass does between two attempts: the evaluation is no longer current."""
        case.validation.append(case.row(900 + len(case.validation), 'val'))

    def test_an_application_whose_pointer_is_written_finishes_whatever_the_evaluation_became(self):
        for point in ('pointer_written', 'receipt_written', 'binding_written', 'pointer_committed'):
            with self.subTest(point=point):
                case = self.fixture()
                self.stop_at(case, point)
                with self.assertRaises(ValueError):
                    A.readable(case.root)
                self.new_evidence(case)
                with self.later(case):
                    self.assertEqual(case.run_apply()['status'], 'applied')
                A.readable(case.root)

    def test_a_resident_busy_after_the_pointer_then_new_evidence_still_activates(self):
        from memvane.learning.context_learning.local_cycle import ResourceDeferred
        case = self.fixture()
        calls = []

        def busy_once(command, record):
            calls.append(command)
            if len(calls) == 2:
                raise ResourceDeferred('apply_busy')
            return case.transport(command, record)
        answer = A.apply_candidate(case.root, case.policy, case.cycle, transport=busy_once)
        self.assertEqual((answer['status'], self.phase(case)), ('apply_deferred', 'pointer_committed'))
        self.new_evidence(case)
        with self.later(case):
            self.assertEqual(case.run_apply()['status'], 'applied')

    def test_a_prepared_application_whose_pointer_was_never_written_starts_again(self):
        case = self.fixture()
        self.stop_at(case, 'prepared')
        first = A.read_journal(case.root)
        self.new_evidence(case)
        answer = case.run_apply()
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', A.STALE))
        self.assertEqual(A.read_journal(case.root), first)
        # Evaluated again (the worker does it on that answer): the journal is formed anew.
        from memvane.learning.context_learning import automatic_staging
        metrics = dict(case.stage['metrics'], validation_questions=len(case.validation),
                       validationEvidenceSha256=automatic_staging._case_evidence(case.validation))
        again = dict(case.stage, metrics=metrics, validationCases=[
            {'projectRoot': r[7], 'requestId': r[1], 'inputSha256': r[2]} for r in case.validation])
        with case.db(None) as conn:
            conn.execute('UPDATE cycles SET evidence=?', (canonical(again),))
        self.assertEqual(case.run_apply()['status'], 'applied')
        self.assertNotEqual(A.read_journal(case.root)['stage_sha256'], first['stage_sha256'])

    def test_a_request_without_a_usable_answer_is_not_the_residents_refusal(self):
        for reason in ('resident_wire_invalid', 'version_mismatch', 'permission_denied'):
            with self.subTest(reason=reason):
                case = self.fixture()

                def unanswered(command, record):
                    raise ValueError(reason)
                answer = A.apply_candidate(case.root, case.policy, case.cycle, transport=unanswered)
                self.assertEqual((answer['status'], answer['reason'], answer['failures']),
                                 ('apply_deferred', reason, 1))
                self.assertEqual(self.phase(case), 'intent')

    def test_failures_are_counted_in_a_row_and_past_the_limit_a_written_pointer_only_reports(self):
        case = self.fixture()
        self.other_failure(case)
        self.assertEqual(A.read_journal(case.root)['failures'], 1)
        # W13 r2 F-9: the count is kept through ``prepared`` (it was reset there, so a failure
        # between the resident's answer and the pointer never reached the limit) and goes back
        # to zero once the pointer is committed.
        self.stop_at_later(case, 'prepared')
        self.assertEqual(A.read_journal(case.root)['failures'], 1)
        self.stop_at_later(case, 'pointer_committed')
        self.assertEqual(A.read_journal(case.root)['failures'], 0)
        case = self.fixture()
        self.stop_at(case, 'pointer_committed')
        for count in range(1, A.FAILURE_LIMIT + 2):
            answer = self.other_failure(case)
            self.assertEqual((answer['status'], answer['failures'], answer['activationPending']),
                             ('apply_blocked', count, count >= A.FAILURE_LIMIT))
            self.assertEqual(A.read_journal(case.root)['reason'], A.IO_ERROR)
            self.assertEqual(self.phase(case), 'pointer_committed')
        with self.later(case):
            self.assertEqual(case.run_apply()['status'], 'applied')

    def stop_at_later(self, case, point):
        with self.later(case):
            self.stop_at(case, point)

    def test_a_judgment_or_an_unusable_journal_ends_the_application_at_once(self):
        for reason in (*A.JUDGED, *A.REFUSED, *A.UNUSABLE):
            with self.subTest(reason=reason):
                case = self.fixture()

                def stop(at):
                    if at == 'intent':
                        raise ValueError(reason)
                answer = case.run_apply(fault=stop)
                self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', reason))
                row = A.read_journal(case.root)
                self.assertEqual((row['phase'], row['reason'], row.get('failures', 0)), ('rejected', reason, 0))


if __name__ == '__main__':
    unittest.main()
