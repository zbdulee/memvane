"""Compile progress crosses the child protocol as one fixed failure detail."""
import unittest

from memvane.app import runtime_dispatch as dispatch


class CompileProgressDetail(unittest.TestCase):
    def test_pending_progress_serializes_to_the_runtime_policy_detail(self):
        result = {'state': 'pending', 'processed': 1, 'reason': 'compile_progress'}
        self.assertEqual(dispatch._deferred_detail(result),
                         'deferred:pending:compile_progress')
