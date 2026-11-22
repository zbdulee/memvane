"""Setup entry dispatch of ``state-cleanup``: the approval flag, the scope flag and the exit code.

The install and provider sides are stand-ins that record how they were
called; what a cleanup removes is ``test_state_cleanup_110`` in the install tests.
"""
import contextlib
import io
import json
import unittest
from unittest import mock

GUARD = 'memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'
RELEASED = 'memvane.install.api.state_cleanup'
EVERYTHING = 'memvane.install.api.state_cleanup_everything'
RUNTIME = 'memvane.provider.api.collect'


def answer(status, changes=0):
    return lambda *args, **kwargs: {'status': status, 'changesApplied': changes, 'approve': kwargs.get('approve')}


class CleanupSetupEntry(unittest.TestCase):
    def run_cli(self, *args):
        from memvane.app import setup_entry
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(['state-cleanup', *args])
        return code, json.loads(out.getvalue())

    def test_the_preview_approves_nothing_and_needs_no_live_write_guard(self):
        with mock.patch(GUARD) as guard, mock.patch(RELEASED, side_effect=answer('planned')) as released, \
                mock.patch(EVERYTHING) as everything, mock.patch(RUNTIME, side_effect=answer('planned')) as runtime:
            code, row = self.run_cli()
        self.assertEqual((code, row['status'], row['approve'], row['indexRuntime']['approve']),
                         (0, 'planned', False, False))
        self.assertEqual((guard.call_count, released.call_count, runtime.call_count, everything.call_count),
                         (0, 1, 1, 0))

    def test_approval_removes_released_installs_and_unused_runtime_generations(self):
        with mock.patch(GUARD) as guard, mock.patch(RELEASED, side_effect=answer('collected', 2)), \
                mock.patch(EVERYTHING) as everything, mock.patch(RUNTIME, side_effect=answer('collected', 3)):
            code, row = self.run_cli('--approve-state-cleanup')
        self.assertEqual((code, row['status'], row['changesApplied'], row['approve'], row['indexRuntime']['approve']),
                         (0, 'collected', 5, True, True))
        guard.assert_called_once_with()
        everything.assert_not_called()

    def test_everything_is_removed_only_with_both_flags(self):
        for flags, approve in ((('--all-state',), False), (('--all-state', '--approve-state-cleanup'), True)):
            with self.subTest(flags=flags), mock.patch(GUARD), mock.patch(RELEASED) as released, \
                    mock.patch(EVERYTHING, side_effect=answer('collected' if approve else 'planned', 3 * approve)), \
                    mock.patch(RUNTIME) as runtime:
                code, row = self.run_cli(*flags)
                self.assertEqual((code, row['approve'], row['changesApplied']), (0, approve, 3 * approve))
                self.assertEqual((released.call_count, runtime.call_count), (0, 0))
        # The approval alone never widens the scope.
        with mock.patch(GUARD), mock.patch(RELEASED, side_effect=answer('collected')), \
                mock.patch(EVERYTHING) as everything, mock.patch(RUNTIME, side_effect=answer('collected')):
            self.run_cli('--approve-state-cleanup')
        everything.assert_not_called()

    def test_whatever_is_left_behind_fails_the_command(self):
        cases = ((answer('partial', 1), answer('collected'), 'partial'),
                 (answer('collected', 1), answer('partial'), 'partial'),
                 (answer('collected', 1), answer('blocked'), 'partial'))
        for released, runtime, status in cases:
            with mock.patch(GUARD), mock.patch(RELEASED, side_effect=released), \
                    mock.patch(RUNTIME, side_effect=runtime):
                code, row = self.run_cli('--approve-state-cleanup')
            self.assertEqual((code, row['status']), (1, status))

    def test_a_machine_without_an_embedding_runtime_still_cleans_installs(self):
        with mock.patch(GUARD), mock.patch(RELEASED, side_effect=answer('collected', 1)), \
                mock.patch(RUNTIME, side_effect=ValueError('laya_embed_runtime_missing')):
            code, row = self.run_cli('--approve-state-cleanup')
        self.assertEqual((code, row['status'], row['changesApplied']), (0, 'collected', 1))
        self.assertEqual(row['indexRuntime'], {'status': 'unavailable', 'reason': 'laya_embed_runtime_missing',
                                               'changesApplied': 0})

    def test_a_refused_live_write_guard_removes_nothing(self):
        with mock.patch(GUARD, side_effect=ValueError('memvane_live_write_requires_cached_plugin')), \
                mock.patch(RELEASED) as released, mock.patch(EVERYTHING) as everything:
            code, row = self.run_cli('--all-state', '--approve-state-cleanup')
        self.assertEqual((code, row['status'], row['changesApplied']), (1, 'rejected', 0))
        self.assertEqual((released.call_count, everything.call_count), (0, 0))
