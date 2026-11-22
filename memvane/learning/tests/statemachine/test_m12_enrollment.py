"""M12 (enrollment setup journal): every phase under every event.

Real writers: ``project_learning_setup.activate`` (with its own fault hook) and
``project_learning_recovery.recover`` on the namespace of a default enrollment.
The ``(absent)`` state is a recovered namespace without its journal: a
recovery leaves the namespace as the enrollment found it.
"""
from __future__ import annotations

import time
from unittest import mock

from memvane.learning import default_enrollment, project_learning_recovery as recovery
from memvane.learning import project_learning_setup as setup
from memvane.learning.context_learning import learning_profile, teacher_state
from memvane.learning.context_learning.state_model import ABSENT, M12
from memvane.learning.tests.renewal_fixture import RenewalFixture


class Stopped(Exception):
    pass


class EnrollmentTable(RenewalFixture):
    def journal(self):
        return self.root / setup.NAME

    def observed(self):
        return self.read(setup.NAME)['phase'] if self.journal().exists() else ABSENT

    def activate(self, stop=None):
        now = int(time.time() * 1000)
        mode = setup.DEFAULT_LOCAL if teacher_state.teacher_off(self.root) else setup.DEFAULT_TEACHER
        plan = setup.prepare(self.project, self.root, expires_at_unix_ms=now + 86400000,
                             not_before_unix_ms=now, mode=mode)
        def fault(reached):
            if reached == stop:
                raise Stopped(reached)
        return setup.activate(plan, accepted_sha256=setup.proposal_sha256(plan), fault=fault)

    def recover(self, stop=False):
        write = recovery._write
        def stopping(path, value):
            write(path, value)
            if stop and value.get('phase') == 'recovering':
                raise Stopped('recovering')
        with mock.patch.object(recovery, '_write', side_effect=stopping):
            return setup.recover(self.project, self.root)

    def build(self, state):
        if self.journal().exists():
            self.recover()
        if state == ABSENT:
            if self.journal().exists():
                self.journal().unlink()
        elif state == 'prepared':
            with self.assertRaises(Stopped):
                self.activate('prepared')
        elif state == 'installing':
            with self.assertRaises(Stopped):
                self.activate('scope_written')
        elif state in ('activated', 'recovering'):
            self.assertEqual(self.activate()['status'], 'activated')
            if state == 'recovering':
                with self.assertRaises(Stopped):
                    self.recover(stop=True)

    def drive(self, state, event):
        listed = (state, event) in M12.transitions
        try:
            if event == 'activate':
                self.activate()
            elif event == 'activate_stops_prepared':
                self.activate('prepared')
            elif event == 'activate_stops_installing':
                self.activate('scope_written')
            elif event == 'recover':
                self.recover()
            elif event == 'recover_stops':
                self.recover(stop=True)
        except Stopped:
            self.assertTrue(listed or state == 'recovering')
        except (ValueError, OSError):
            self.assertFalse(listed)  # A refusal: nothing is written.

    def test_every_phase_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M12.states:
            for event in M12.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(state, event)
                    self.assertEqual(self.observed(), M12.next(state, event))
                cells += 1
                if (state, event) in M12.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M12.states), len(M12.events), cells), (6, 5, 30))
        self.assertEqual(driven, set(M12.transitions))

    def test_an_unfinished_journal_refuses_a_new_activation_with_its_reason(self):
        for state, reason in (('prepared', 'setup_existing_transaction'),
                              ('recovering', 'setup_existing_learning_config'),
                              ('activated', 'setup_existing_learning_config')):
            with self.subTest(state=state):
                self.build(state)
                with self.assertRaisesRegex(ValueError, reason):
                    self.activate()

    def test_a_recovery_puts_the_settings_and_the_namespace_back(self):
        self.build('recovered')
        settings, profile = self.settings.read_bytes(), learning_profile.present(self.root)
        self.assertFalse(profile)
        for state in ('prepared', 'installing', 'activated', 'recovering'):
            with self.subTest(state=state):
                self.build(state)
                self.assertIn(self.recover()['status'], ('recovered',))
                self.assertEqual(self.settings.read_bytes(), settings)
                self.assertFalse(learning_profile.present(self.root))
                self.assertEqual(self.recover(), {'status': 'already_recovered'})

    def test_the_default_enrollment_recovers_an_unfinished_journal_before_it_activates(self):
        for state in ('prepared', 'installing', 'recovering', 'recovered'):
            with self.subTest(state=state):
                self.build(state)
                default_enrollment._activate_profile(self.project, self.root)
                self.assertEqual(self.observed(), 'activated')
                self.assertTrue(learning_profile.present(self.root))
