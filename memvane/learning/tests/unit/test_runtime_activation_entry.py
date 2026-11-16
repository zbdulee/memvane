import unittest
from unittest import mock


class RuntimeActivationTests(unittest.TestCase):
    def test_resource_denial_never_activates_generation(self):
        from memvane.learning import runtime_activation
        with mock.patch.object(runtime_activation, '_memory_admission', return_value=False), \
             mock.patch.object(runtime_activation, '_activate') as activate:
            with self.assertRaisesRegex(runtime_activation.ResourceDeferred,
                                        'learning_prepare_memory_pressure'):
                runtime_activation.activate('/tmp/venv/bin/python', '/tmp/model')
        activate.assert_not_called()


    def test_success_holds_and_releases_existing_prepare_lease(self):
        from memvane.learning import runtime_activation
        with mock.patch.object(runtime_activation, '_memory_admission', return_value=True), \
             mock.patch('memvane.learning.context_learning.selector_heal._prepare_lease', return_value=9), \
             mock.patch.object(runtime_activation.os, 'close') as close, \
             mock.patch.object(runtime_activation, '_activate', return_value={'status': 'activated'}) as activate:
            self.assertEqual(runtime_activation.activate('/tmp/venv/bin/python', '/tmp/model')['status'],
                             'activated')
        activate.assert_called_once_with('/tmp/venv/bin/python', '/tmp/model')
        close.assert_called_once_with(9)
