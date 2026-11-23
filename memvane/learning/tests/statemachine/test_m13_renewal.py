"""M13 (approval renewal): every intent state under every event.

Real writer: ``default_renewal.renew`` on an enrolled default namespace. An
interruption is an exception raised right after one target write.
"""
from __future__ import annotations

from unittest import mock

from memvane.learning import default_renewal as renewal
from memvane.learning.context_learning import learning_profile
from memvane.learning.context_learning.state_model import ABSENT, M13
from memvane.learning.tests.renewal_fixture import RenewalFixture


class RenewalTable(RenewalFixture):
    teacher_off_switch = True

    def intent(self):
        return self.root / learning_profile.RENEWAL

    def observed(self):
        return self.read(learning_profile.RENEWAL)['phase'] if self.intent().exists() else ABSENT

    def stopped(self):
        """``renew`` ended by an interruption right after its next target write."""
        write = renewal._restore_bytes
        def once(*args, **kwargs):
            write(*args, **kwargs)
            raise InterruptedError('synthetic write interruption')
        with mock.patch.object(renewal, '_restore_bytes', side_effect=once), \
                self.assertRaises(InterruptedError):
            self.renew()

    def build(self, state):
        for name, raw in self.original.items():
            (self.root / name).write_bytes(raw)
        if self.intent().exists():
            self.intent().unlink()
        self.select(version='1.0.15', marker='A')
        if state != ABSENT:
            self.interrupted(2)  # The 1.0.16 bundle, two of its targets written.
        if state == 'rolling_back':
            self.select(version='1.0.17', marker='C')
            self.stopped()

    def drive(self, state, event):
        if event == 'renew':
            self.renew()
        elif event == 'publish_interrupted':
            if state == ABSENT:
                self.interrupted(1)
            else:
                self.stopped()  # A rolling-back intent only restores: no publication runs.
        elif event == 'rollback_interrupted':
            if state == ABSENT:
                # Nothing to restore: the pass with nothing new writes nothing.
                self.assertEqual(self.renew(), {'status': 'current'})
            else:
                self.select(version='1.0.17', marker='C')
                self.stopped()

    def test_every_intent_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M13.states:
            for event in M13.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(state, event)
                    self.assertEqual(self.observed(), M13.next(state, event))
                cells += 1
                if (state, event) in M13.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M13.states), len(M13.events), cells), (3, 3, 9))
        self.assertEqual(driven, set(M13.transitions))

    def test_what_each_finished_pass_answers_and_leaves(self):
        self.build(ABSENT)
        self.assertEqual(self.renew(), {'status': 'current'})
        self.assertEqual(self.snapshot_targets(), self.original)
        self.select()
        self.assertEqual(self.renew(), {'status': 'renewed'})
        renewed = self.snapshot_targets()
        self.assertNotEqual(renewed, self.original)
        self.assertEqual(self.renew(), {'status': 'current'})
        # An intent whose target is still the recorded one is finished, not planned again.
        self.build('intent')
        self.assertEqual(self.renew(), {'status': 'renewed'})
        self.assertEqual(self.snapshot_targets(), renewed)
        # A rolling-back intent restores the old bytes first, then renews to what is installed.
        self.build('rolling_back')
        self.assertEqual(self.renew(), {'status': 'renewed'})
        self.assertNotIn(self.snapshot_targets(), (self.original, renewed))
        # A bundle older than the one that wrote the intent leaves it alone.
        self.build('rolling_back')
        before = self.snapshot_targets()
        self.select(version='1.0.15', marker='A')
        with self.assertRaisesRegex(ValueError, 'renewal_owner_newer'):
            self.renew()
        self.assertEqual((self.observed(), self.snapshot_targets()), ('rolling_back', before))

    def test_a_refusal_keeps_the_intent_and_every_byte(self):
        self.build('intent')
        self.select(marker='different B bytes')
        before = self.snapshot_targets()
        with self.assertRaisesRegex(ValueError, 'renewal_same_version_changed'):
            self.renew()
        self.assertEqual((self.observed(), self.snapshot_targets()), ('intent', before))
        self.build('intent')
        (self.root / 'shared-policy.json').write_bytes(b'{"other":1}\n')
        before = self.snapshot_targets()
        with self.assertRaisesRegex(ValueError, 'renewal_mixed_state'):
            self.renew()
        self.assertEqual((self.observed(), self.snapshot_targets()), ('intent', before))

    def test_a_teacher_enable_intent_holds_the_renewal(self):
        for state in M13.states:
            with self.subTest(state=state):
                self.build(state)
                before = self.snapshot_targets()
                (self.root / learning_profile.TEACHER_ENABLE).write_text('{}\n', encoding='utf8')
                self.addCleanup(lambda: (self.root / learning_profile.TEACHER_ENABLE).unlink(missing_ok=True))
                self.select(version='1.0.17', marker='C')
                self.assertEqual(self.renew(), {'status': 'teacher_enable_pending'})
                self.assertEqual((self.observed(), self.snapshot_targets()), (state, before))
                (self.root / learning_profile.TEACHER_ENABLE).unlink()
