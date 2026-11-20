"""1.0.21 setup skill ``teacher-off``/``teacher-on`` (plan §11-4, the G-14 command part).

The commands run as ``learning.api.learning_main`` subcommands (what
``setup_entry`` dispatches to; its dispatch is
``memvane/app/tests/unit/test_setup_entry_teacher_121.py``) on ``RenewalFixture`` installs: real selection stand-in, enrollment, setup
journal and namespace files. The switch is the namespace's
``teacher-switch.json``; an enabled profile keeps its bytes, and a
default-local install owes no enable while it is off.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from unittest import mock

from memvane.learning import api as learning_api
from memvane.learning import selector_maintenance
from memvane.learning.context_learning import teacher_consent, teacher_state
from memvane.learning.context_learning.runtime_setup import learning_state_base
from memvane.learning.tests.renewal_fixture import RenewalFixture


class SwitchMixin:
    def entry(self, action, *, plan=False):
        """(exit code, printed result) of ``learning_main([action, '--project', P, ...])``."""
        out = io.StringIO()
        with mock.patch.dict(os.environ), redirect_stdout(out):
            os.environ.pop(teacher_state.ENV_SWITCH, None)
            code = learning_api.learning_main([action, '--project', str(self.project),
                                               *(['--plan'] if plan else [])])
        return code, json.loads(out.getvalue())

    def ok(self, action, **options):
        code, result = self.entry(action, **options)
        self.assertEqual(code, 0, result)
        return result

    def switch_bytes(self):
        path = self.root / teacher_state.SWITCH_NAME
        return path.read_bytes() if path.exists() else None


class TeacherSwitch(SwitchMixin, RenewalFixture):
    """A default-teacher install (the product default)."""

    def test_off_then_on_writes_only_the_switch(self):
        consent = (self.root / teacher_consent.NAME).read_bytes()
        result = self.ok('teacher-off')
        self.assertEqual((result['status'], result['changesApplied'], result['teacherOff']),
                         ('teacher_off', 1, True))
        self.assertEqual(result['namespaceRoot'], str(self.root))
        switch = self.root / teacher_state.SWITCH_NAME
        self.assertEqual(switch.stat().st_mode & 0o777, 0o600)
        self.assertTrue(teacher_state.teacher_off(self.root, environ={}))
        # An enabled profile is not turned back: grant, profile, journal and consent stay.
        self.assertEqual(self.snapshot_targets(), self.original)
        self.assertEqual((self.root / teacher_consent.NAME).read_bytes(), consent)
        result = self.ok('teacher-on')
        self.assertEqual((result['status'], result['teacherOff']), ('teacher_on', False))
        self.assertFalse(teacher_state.teacher_off(self.root, environ={}))
        self.assertEqual(self.snapshot_targets(), self.original)

    def test_environment_switch_still_wins_after_teacher_on(self):
        self.assertEqual(self.ok('teacher-on')['status'], 'teacher_on')
        self.assertTrue(teacher_state.teacher_off(self.root, environ={'MEMVANE_TEACHER': 'off'}))
        self.assertFalse(teacher_state.teacher_off(self.root, environ={'MEMVANE_TEACHER': 'on'}))

    def test_unwritten_switch_fails_and_changes_nothing(self):
        before = self.switch_bytes()
        with mock.patch.object(teacher_state, 'write_switch', return_value=False):
            code, result = self.entry('teacher-off')
        self.assertEqual((code, result), (1, {'status': 'failed', 'reason': 'teacher_switch_unwritten'}))
        self.assertEqual(self.switch_bytes(), before)

    def test_plan_is_refused_for_the_switches(self):
        before = self.switch_bytes()
        for action in ('teacher-off', 'teacher-on'):
            code, result = self.entry(action, plan=True)
            self.assertEqual((code, result['reason']), (1, 'teacher_setup_action_invalid'))
        out = io.StringIO()
        with redirect_stdout(out):
            code = learning_api.learning_main(['teacher-off'])
        self.assertEqual((code, json.loads(out.getvalue())['reason']), (1, 'teacher_setup_action_invalid'))
        self.assertEqual(self.switch_bytes(), before)

    def test_restore_plan_still_dispatches(self):
        result = self.ok('restore-teacher-enable', plan=True)
        self.assertEqual((result['status'], result['changesApplied']), ('no_enable_intent', 0))


class TeacherSwitchLocal(SwitchMixin, RenewalFixture):
    """A default-local install that still owes the enable: off stops the request."""
    teacher_off_switch = True

    def setUp(self):
        super().setUp()
        (self.root / teacher_state.SWITCH_NAME).unlink()

    def test_off_stops_the_enable_request_and_on_restores_it(self):
        self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))
        self.assertEqual(self.ok('teacher-off')['status'], 'teacher_off')
        with mock.patch.dict(os.environ):
            os.environ.pop(teacher_state.ENV_SWITCH, None)
            self.assertFalse(selector_maintenance._teacher_enable_needed(self.decision))
        self.assertEqual(self.snapshot_targets(), self.original)
        self.assertEqual(self.ok('teacher-on')['status'], 'teacher_on')
        self.assertTrue(selector_maintenance._teacher_enable_needed(self.decision))


class SwitchNamespace(RenewalFixture):
    """``set_switch`` before enrollment and on an unsafe path."""

    def test_switch_creates_a_missing_private_namespace(self):
        base = learning_state_base()
        root = base / 'live-selection' / ('0' * 64)
        self.assertFalse(root.exists())
        result = teacher_state.set_switch(root, base, False)
        self.assertEqual(result['status'], 'teacher_off')
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(teacher_state.switch_off(root))

    def test_symlinked_or_foreign_namespace_is_refused(self):
        base = learning_state_base()
        target = self.home / 'elsewhere'
        target.mkdir(mode=0o700)
        link = base / 'live-selection' / ('1' * 64)
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'teacher_switch_namespace_unsafe'):
            teacher_state.set_switch(link, base, False)
        with self.assertRaisesRegex(ValueError, 'teacher_switch_namespace_unsafe'):
            teacher_state.set_switch(self.home / 'other' / ('2' * 64), base, False)
        self.assertEqual(list(target.iterdir()), [])
