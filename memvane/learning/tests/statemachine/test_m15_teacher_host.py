"""M15 (teacher host): every host state under every pass stimulus and the wait expiry.

A stimulus is one real teacher pass over one queued case (fake host CLIs) or
the passing of a wait's expiry. It names the model events it attempts in
order; the expected state is the model's ``next`` folded over them. The host
state is read back from ``teacher-host-state.json`` the way a pass reads it.
"""
from __future__ import annotations

from memvane.learning.context_learning import shared, teacher_state
from memvane.learning.context_learning.state_model import M15
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture

HOST = 'claude'
TIMED = {'teacher_provider_waiting': 'provider_waiting', 'teacher_host_failing': 'failing'}
# stimulus -> the model events it attempts, in order.
STIMULI = {
    'pass_answered': ['host_ready'],
    'pass_cli_missing': ['host_not_ready'],
    'pass_provider_refused': ['host_ready', 'provider_refused'],
    'pass_failure_below_limit': ['host_ready'],
    'pass_third_failure': ['host_ready', 'failure_limit'],
    'wait_expires': ['wait_expired'],
}


def fold(state, events):
    for event in events:
        state = M15.next(state, event)
    return state


class TeacherHostTable(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.path, self.serial = self.environ['PATH'], 0

    def host(self):
        """The host's state as the next pass would read it."""
        state = self.host_state()
        row = state['hosts'][HOST]
        waiting = teacher_state.host_waiting(state, HOST)
        if waiting:
            return TIMED.get(row['reason'], 'failing')
        # An expired wait keeps its reason until the next pass clears it.
        return 'resting' if row['reason'] and row['reason'] not in TIMED else 'ready'

    def failures(self):
        return self.host_state()['hosts'][HOST]['consecutiveFailures']

    def one_pass(self, scenario=None, *, cli=True):
        # One case per pass: a case an earlier pass left queued would be called too.
        with shared._db(self.root) as db:
            db.execute("DELETE FROM cases WHERE status='queued'")
        self.serial += 1
        self.add('case-%d' % self.serial)
        self.scenario(HOST, scenario or {})
        self.environ['PATH'] = self.path if cli else '/usr/bin:/bin'
        try:
            return self.run_pass()
        finally:
            self.environ['PATH'] = self.path

    def set_failures(self, count):
        state = self.host_state()
        state['hosts'][HOST]['consecutiveFailures'] = count
        self.assertTrue(teacher_state.write_state(self.root, state))

    def build(self, state):
        (self.root / teacher_state.STATE_NAME).unlink(missing_ok=True)
        if state == 'resting':
            self.one_pass(cli=False)
        elif state == 'provider_waiting':
            self.one_pass({'mode': 'quota'})
        elif state == 'failing':
            self.set_failures(teacher_state.FAILURE_LIMIT - 1)
            self.one_pass({'mode': 'exit'})

    def stimulate(self, name):
        if name == 'pass_answered':
            self.one_pass()
        elif name == 'pass_cli_missing':
            self.one_pass(cli=False)
        elif name == 'pass_provider_refused':
            self.one_pass({'mode': 'quota'})
        elif name == 'pass_failure_below_limit':
            self.set_failures(0)
            self.one_pass({'mode': 'exit'})
        elif name == 'pass_third_failure':
            self.set_failures(teacher_state.FAILURE_LIMIT - 1)
            self.one_pass({'mode': 'exit'})
        elif name == 'wait_expires':
            self.expire_waits()

    def test_every_host_state_under_every_stimulus_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M15.states:
            for name, events in STIMULI.items():
                with self.subTest(state=state, stimulus=name):
                    self.build(state)
                    self.assertEqual(self.host(), state)
                    self.stimulate(name)
                    self.assertEqual(self.host(), fold(state, events))
                cells += 1
                at = state
                for event in events:
                    if (at, event) in M15.transitions:
                        driven.add((at, event))
                    at = M15.next(at, event)
        self.assertEqual(cells, len(M15.states) * len(STIMULI))
        self.assertEqual((len(M15.states), len(STIMULI)), (4, 6))
        self.assertEqual(driven, set(M15.transitions))
        self.assertEqual({event for events in STIMULI.values() for event in events}, set(M15.events))

    def test_the_pairs_no_pass_reaches_are_the_calls_of_a_resting_host(self):
        seen = set()
        for state in M15.states:
            for events in STIMULI.values():
                at = state
                for event in events:
                    seen.add((at, event))
                    at = M15.next(at, event)
        # A resting host is not called, so its call cannot be refused or fail.
        self.assertEqual(set(M15.cells()) - seen,
                         {('resting', 'provider_refused'), ('resting', 'failure_limit')})
        self.assertEqual((len(M15.cells()), len(seen)), (20, 18))
        self.add('rested')
        self.environ['PATH'] = '/usr/bin:/bin'
        try:
            for scenario in ({'mode': 'quota'}, {'mode': 'exit'}):
                self.scenario(HOST, scenario)
                self.run_pass()
                self.assertEqual((self.host(), self.runs(HOST), self.failures()), ('resting', 0, 0))
        finally:
            self.environ['PATH'] = self.path

    def test_failures_below_the_limit_only_count_and_an_answer_resets_them(self):
        self.one_pass({'mode': 'exit'})
        self.one_pass({'mode': 'exit'})
        self.assertEqual((self.host(), self.failures()), ('ready', teacher_state.FAILURE_LIMIT - 1))
        self.one_pass()
        self.assertEqual((self.host(), self.failures()), ('ready', 0))
        for _ in range(teacher_state.FAILURE_LIMIT):
            self.one_pass({'mode': 'exit'})
        self.assertEqual((self.host(), self.failures()), ('failing', teacher_state.FAILURE_LIMIT))
        # The count survives the expiry: one more failure rests the host again.
        self.expire_waits()
        self.assertEqual(self.host(), 'ready')
        self.one_pass({'mode': 'exit'})
        self.assertEqual(self.host(), 'failing')
