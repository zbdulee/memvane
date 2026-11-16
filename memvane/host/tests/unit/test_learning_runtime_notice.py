import unittest
from unittest import mock


class RuntimeNoticeTests(unittest.TestCase):
    def test_session_notice_is_added_only_for_inactive_runtime(self):
        from memvane.host.host_response import _learning_runtime_notice
        with mock.patch('memvane.learning.api.learning_runtime_proof_failed', return_value=False), \
             mock.patch('memvane.learning.api.learning_runtime_needs_activation',
                        create=True, return_value=True):
            line = _learning_runtime_notice('existing')
        self.assertIn('existing\n[memvane]', line)
        self.assertIn('setup', line)
        with mock.patch('memvane.learning.api.learning_runtime_proof_failed', return_value=False), \
             mock.patch('memvane.learning.api.learning_runtime_needs_activation',
                        create=True, return_value=False):
            self.assertEqual(_learning_runtime_notice('existing'), 'existing')

    def test_failed_compatibility_has_actionable_notice(self):
        from memvane.host.host_response import _learning_runtime_notice
        with mock.patch('memvane.learning.api.learning_runtime_message',
                        return_value='학습 런타임의 첫 활성화에 실패했습니다. setup skill로 확인하세요.'):
            result = _learning_runtime_notice('existing')
        self.assertIn('첫 활성화에 실패', result)
        self.assertIn('setup skill', result)
