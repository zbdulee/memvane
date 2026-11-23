"""M14 (default teacher enable): every intent state under every event.

Real writers: ``default_teacher_enable.run`` and ``restore`` on a default-local
enrollment. An interruption is the transaction's own fault hook.
"""
from __future__ import annotations

from memvane.learning import default_teacher_enable as enable, project_learning_setup as setup
from memvane.learning.context_learning import teacher_consent, teacher_state
from memvane.learning.context_learning.state_model import ABSENT, M14
from memvane.learning.tests.integration import test_default_teacher_enable_121 as enable_tests

Interrupted = enable_tests.Interrupted


class EnableTable(enable_tests.LocalFixture):
    def observed(self):
        path = self.root / enable.INTENT
        return self.read(enable.INTENT)['phase'] if path.exists() else ABSENT

    def rollback_stops(self):
        """The precondition is gone and the rollback is interrupted after its first write."""
        self.assertTrue(teacher_state.write_switch(self.root, False))
        def fault(name):
            if name.startswith('rollback:'):
                raise Interrupted(name)
        try:
            return self.run_enable(fault=fault)
        finally:
            (self.root / teacher_state.SWITCH_NAME).unlink()

    def build(self, state):
        self.reset()
        if state != ABSENT:
            with self.assertRaises(Interrupted):
                self.run_enable(fault=self.at(setup.NAME))
        if state == 'rolling_back':
            with self.assertRaises(Interrupted):
                self.rollback_stops()

    def drive(self, state, event):
        if event == 'run':
            self.run_enable()
        elif event == 'restore':
            enable.restore(self.root, self.project)
        elif event == 'forward_interrupted':
            if state != 'rolling_back':  # A rolling-back intent writes no new bytes.
                with self.assertRaises(Interrupted):
                    self.run_enable(fault=self.at(teacher_consent.NAME))
        elif event == 'rollback_interrupted':
            if state == ABSENT:
                # Nothing to roll back: the blocked start answers and writes nothing.
                self.assertEqual(self.rollback_stops(), {'status': 'deferred', 'reason': 'teacher_off'})
            else:
                with self.assertRaises(Interrupted):
                    self.rollback_stops()

    def test_every_intent_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M14.states:
            for event in M14.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(state, event)
                    self.assertEqual(self.observed(), M14.next(state, event))
                cells += 1
                if (state, event) in M14.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M14.states), len(M14.events), cells), (3, 4, 12))
        self.assertEqual(driven, set(M14.transitions))

    def test_what_each_finished_pass_answers_and_leaves(self):
        self.build(ABSENT)
        self.assertEqual(self.run_enable()['status'], 'enabled')
        new = self.all()
        self.assertNotEqual(new, self.old)
        self.build('intent')
        self.assertEqual(self.run_enable()['status'], 'enabled')
        self.assertEqual(self.all(), new)
        self.build('rolling_back')
        self.assertEqual(self.run_enable(), {'status': 'rolled_back', 'reason': 'teacher_enable_rolled_back'})
        self.assertEqual(self.all(), self.old)
        for state in ('intent', 'rolling_back'):
            self.build(state)
            self.assertEqual(enable.restore(self.root, self.project)['status'], 'restored')
            self.assertEqual(self.all(), self.old)
        self.build(ABSENT)
        self.assertEqual(enable.restore(self.root, self.project)['status'], 'no_enable_intent')

    def test_an_older_bundle_and_a_mixed_state_write_nothing(self):
        for state in ('intent', 'rolling_back'):
            with self.subTest(state=state):
                self.build(state)
                before = self.all()
                self.assertEqual(self.run_enable(version='1.0.14'), {'status': 'deferred', 'reason': 'owner_newer'})
                self.assertEqual((self.observed(), self.all()), (state, before))
                (self.root / 'shared-policy.json').write_bytes(b'{"other":1}\n')
                before = self.all()
                with self.assertRaisesRegex(ValueError, 'teacher_enable_mixed_state'):
                    self.run_enable()
                self.assertEqual((self.observed(), self.all()), (state, before))

    def test_five_failures_with_one_reason_stop_the_requests_until_the_version_changes(self):
        self.build(ABSENT)
        for count in range(1, teacher_state.ENABLE_FAILURE_LIMIT + 1):
            self.assertFalse(teacher_state.enable_requests_stopped(teacher_state.read_state(self.root), '1.0.15'))
            self.assertTrue(enable.count_failure(self.root, 'teacher_enable_inputs_changed', '1.0.15'))
            self.assertEqual(teacher_state.read_state(self.root)['enable']['failures'], count)
        self.assertTrue(teacher_state.enable_requests_stopped(teacher_state.read_state(self.root), '1.0.15'))
        self.assertFalse(teacher_state.enable_requests_stopped(teacher_state.read_state(self.root), '1.0.16'))
        self.assertTrue(enable.count_failure(self.root, 'teacher_enable_target_changed', '1.0.15'))
        self.assertEqual(teacher_state.read_state(self.root)['enable']['failures'], 1)
