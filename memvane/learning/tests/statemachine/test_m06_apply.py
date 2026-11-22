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
    'one_other_failure': ['intent_formed'],
    'five_other_failures': ['intent_formed', 'ended'],
    'momentary_resource_state': ['intent_formed'],
    'stale_evaluation': [],
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
            stage = dict(case.stage, metrics=dict(case.stage['metrics'], evaluatedAt=1))
            with case.db(None) as conn:
                before = conn.execute('SELECT evidence FROM cycles').fetchone()[0]
                conn.execute('UPDATE cycles SET evidence=?', (canonical(stage),))
            try:
                with self.later(case):
                    return case.run_apply()
            finally:
                with case.db(None) as conn:
                    conn.execute('UPDATE cycles SET evidence=?', (before,))
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
                    if state == 'pointer_committed' and name in ('resident_refuses', 'five_other_failures',
                                                                'one_other_failure'):
                        # The pointer is already the candidate's: the attempt cannot end the
                        # application, and no later attempt forms the intent again.
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
        self.assertEqual((len(M6.states), len(STIMULI)), (6, 6))
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
                             ('apply_deferred', 'fixture socket', count))
            row = A.read_journal(case.root)
            self.assertEqual((row['phase'], row['failures']), ('intent', count))
            waits.append(row['retry_at'])
            # Before its time the attempt is not made at all.
            before = list(case.events)
            self.assertEqual(case.run_apply()['status'], 'apply_deferred')
            self.assertEqual(case.events, before)
        self.assertEqual(waits, sorted(waits))
        answer = self.other_failure(case)
        self.assertEqual((answer['status'], answer['reason']), ('apply_blocked', 'fixture socket'))
        row = A.read_journal(case.root)
        self.assertEqual((row['phase'], row['reason'], row['failures']),
                         ('rejected', 'fixture socket', A.FAILURE_LIMIT))
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
