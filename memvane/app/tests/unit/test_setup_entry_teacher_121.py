"""1.0.21 setup entry dispatch of ``teacher-off``, ``teacher-on`` and ``restore-teacher-enable``.

The learning side (the ``learning.api.learning_main`` subcommands) is
replaced here by a stand-in that prints a result; its behavior on real
namespaces is ``test_teacher_switch_121`` in the learning tests.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

GUARD = 'memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'


def printing(result, code=0):
    calls = []

    def learning_main(argv):
        calls.append(list(argv))
        print(json.dumps(result))
        return code
    return learning_main, calls


class TeacherSetupEntry(unittest.TestCase):
    def run_cli(self, *args):
        from memvane.app import setup_entry
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(list(args))
        return code, json.loads(out.getvalue())

    def test_switches_dispatch_to_learning_main_as_writes(self):
        for action, status in (('teacher-off', 'teacher_off'), ('teacher-on', 'teacher_on')):
            fake, calls = printing({'status': status, 'changesApplied': 1})
            with self.subTest(action=action), mock.patch(GUARD) as guard, \
                    mock.patch('memvane.learning.api.learning_main', side_effect=fake):
                code, row = self.run_cli(action, '--project', '/tmp/project')
                self.assertEqual((code, row), (0, {'status': status, 'changesApplied': 1}))
                self.assertEqual(calls, [[action, '--project', '/tmp/project']])
                guard.assert_called_once_with()

    def test_restore_plan_is_read_only_and_passes_plan(self):
        fake, calls = printing({'status': 'restore_planned', 'changesApplied': 0})
        with mock.patch(GUARD) as guard, mock.patch('memvane.learning.api.learning_main', side_effect=fake):
            code, row = self.run_cli('restore-teacher-enable', '--project', '/tmp/project', '--plan')
        self.assertEqual((code, row['status']), (0, 'restore_planned'))
        self.assertEqual(calls, [['restore-teacher-enable', '--project', '/tmp/project', '--plan']])
        guard.assert_not_called()

    def test_deferred_restore_keeps_the_retry_exit_code(self):
        fake, _ = printing({'status': 'deferred', 'reason': 'worker_busy', 'changesApplied': 0})
        with mock.patch(GUARD), mock.patch('memvane.learning.api.learning_main', side_effect=fake):
            code, row = self.run_cli('restore-teacher-enable', '--project', '/tmp/project')
        self.assertEqual((code, row['reason']), (75, 'worker_busy'))

    def test_learning_failure_is_a_rejection_with_exit_one(self):
        fake, _ = printing({'status': 'failed', 'reason': 'teacher_switch_unwritten'}, code=1)
        with mock.patch(GUARD), mock.patch('memvane.learning.api.learning_main', side_effect=fake):
            code, row = self.run_cli('teacher-off', '--project', '/tmp/project')
        self.assertEqual((code, row['status'], row['reason'], row['changesApplied']),
                         (1, 'rejected', 'teacher_switch_unwritten', 0))

    def test_switch_requires_a_project(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli('teacher-off')


if __name__ == '__main__':
    unittest.main()
