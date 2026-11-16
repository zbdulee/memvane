import unittest
from unittest import mock


class RuntimeNoticeTests(unittest.TestCase):
    def test_session_notice_is_added_only_for_inactive_runtime(self):
        from memvane.host.host_response import _learning_runtime_notice
        with mock.patch('memvane.learning.api.learning_runtime_needs_activation',
                        create=True, return_value=True):
            line = _learning_runtime_notice('existing')
        self.assertIn('existing\n[memvane]', line)
        self.assertIn('setup', line)
        with mock.patch('memvane.learning.api.learning_runtime_needs_activation',
                        create=True, return_value=False):
            self.assertEqual(_learning_runtime_notice('existing'), 'existing')

    def test_failed_compatibility_has_actionable_notice(self):
        from memvane.host.host_response import _learning_runtime_notice
        with mock.patch('memvane.learning.api.learning_runtime_proof_failed', return_value=True):
            result = _learning_runtime_notice('existing')
        self.assertIn('호환성 검사에 실패', result)
        self.assertIn('setup skill', result)
