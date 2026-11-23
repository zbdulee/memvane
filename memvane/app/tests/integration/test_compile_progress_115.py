"""Compile progress crosses the child protocol as one fixed failure detail."""
import json
import unittest
from unittest import mock

from memvane.app import runtime_dispatch as dispatch


class CompileProgressDetail(unittest.TestCase):
    def test_pending_progress_serializes_to_the_runtime_policy_detail(self):
        result = {'state': 'pending', 'processed': 1, 'reason': 'compile_progress'}
        self.assertEqual(dispatch._deferred_detail(result),
                         'deferred:pending:compile_progress')


class CompileProgressBacklog(unittest.TestCase):
    def failure(self, result):
        from memvane.runtime.types import DispatchFailure
        child = mock.Mock(returncode=0, stdout=json.dumps(result).encode(), stderr=b'')
        with mock.patch.object(dispatch, '_run_child', return_value=child), \
                mock.patch('memvane.runtime.api.background_bundle'), \
                self.assertRaises(DispatchFailure) as caught:
            dispatch._run_target('laya-compile', '/project', {'project': '/project'}, None)
        return caught.exception

    def test_the_backlog_a_pass_left_travels_with_the_failure(self):
        base = {'state': 'pending', 'reason': 'compile_progress'}
        for value, expected in ((17, 17), (0, 0), (None, None), (-1, None), ('17', None), (True, None)):
            with self.subTest(value=value):
                failure = self.failure({**base, 'backlog': value})
                self.assertEqual((failure.detail, failure.backlog), ('deferred:pending:compile_progress', expected))
        self.assertIsNone(self.failure(base).backlog)
