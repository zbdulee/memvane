"""Setup entry dispatch of ``restore-previous-model``: the plan flag, the live-write guard and the exit code.

The learning side (the ``learning.api.learning_main`` subcommand) is a
stand-in that prints a result; what a restore writes is
``test_model_rollback_110`` in the learning tests.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

GUARD = 'memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'
LEARNING = 'memvane.learning.api.learning_main'


def printing(result):
    calls = []

    def learning_main(argv):
        calls.append(list(argv))
        print(json.dumps(result))
        return 0
    return learning_main, calls


class RollbackSetupEntry(unittest.TestCase):
    def run_cli(self, *args):
        from memvane.app import setup_entry
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(['restore-previous-model', *args])
        return code, json.loads(out.getvalue())

    def test_the_plan_is_read_only(self):
        fake, calls = printing({'status': 'rollback_planned', 'changesApplied': 0})
        with mock.patch(GUARD) as guard, mock.patch(LEARNING, side_effect=fake):
            code, row = self.run_cli('--project', '/tmp/project', '--plan')
        self.assertEqual((code, row['status']), (0, 'rollback_planned'))
        self.assertEqual(calls, [['restore-previous-model', '--project', '/tmp/project', '--plan']])
        guard.assert_not_called()

    def test_a_restore_is_a_guarded_write_and_its_result_sets_the_exit_code(self):
        for status, code in (('restored', 0), ('already_restored', 0), ('nothing_to_restore', 0),
                             ('rollback_blocked', 1), ('rollback_interrupted', 1), ('rollback_deferred', 75)):
            result = {'status': status, 'changesApplied': int(status == 'restored')}
            fake, calls = printing(result)
            with self.subTest(status=status), mock.patch(GUARD) as guard, mock.patch(LEARNING, side_effect=fake):
                self.assertEqual(self.run_cli('--project', '/tmp/project'), (code, result))
                self.assertEqual(calls, [['restore-previous-model', '--project', '/tmp/project']])
                guard.assert_called_once_with()

    def test_a_failed_learning_command_or_a_refused_guard_is_rejected(self):
        fake, _calls = printing({'status': 'failed', 'reason': 'teacher_setup_action_invalid'})
        with mock.patch(GUARD), mock.patch(LEARNING, side_effect=fake):
            code, row = self.run_cli('--project', '/tmp/project')
        self.assertEqual((code, row['status'], row['reason']), (1, 'rejected', 'teacher_setup_action_invalid'))
        with mock.patch(GUARD, side_effect=ValueError('memvane_live_write_requires_cached_plugin')), \
                mock.patch(LEARNING) as learning:
            code, row = self.run_cli('--project', '/tmp/project')
        self.assertEqual((code, row['status'], row['changesApplied']), (1, 'rejected', 0))
        learning.assert_not_called()
