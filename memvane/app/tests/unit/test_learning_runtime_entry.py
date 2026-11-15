import contextlib
import io
import json
import unittest
from unittest import mock


class RuntimeEntryTests(unittest.TestCase):
    def run_cli(self, *args):
        from memvane.app import setup_entry
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = setup_entry.main(list(args))
        return code, json.loads(out.getvalue())


    def test_activation_requires_explicit_approval(self):
        with mock.patch('memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'):
            code, row = self.run_cli('activate-learning-runtime',
                                     '--learning-runtime-executable', '/tmp/venv/bin/python',
                                     '--learning-model-dir', '/tmp/model')
        self.assertEqual(code, 1)
        self.assertEqual(row['reason'], 'learning_runtime_activation_approval_required')


    def test_cli_passes_explicit_inputs_to_learning_owner(self):
        with mock.patch('memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'), \
             mock.patch('memvane.learning.api.activate_managed_runtime', create=True,
                        return_value={'status': 'activated'}) as activate:
            code, row = self.run_cli('activate-learning-runtime',
                                     '--learning-runtime-executable', '/tmp/venv/bin/python',
                                     '--learning-model-dir', '/tmp/model',
                                     '--approve-learning-runtime-activation')
        self.assertEqual((code, row['status']), (0, 'activated'))
        activate.assert_called_once_with('/tmp/venv/bin/python', '/tmp/model')


    def test_deferred_admission_is_reported_without_changes(self):
        from memvane.kernel.resource_deferred import ResourceDeferred
        with mock.patch('memvane.kernel.memvane_execution_guard.require_cached_plugin_for_live_write'), \
             mock.patch('memvane.learning.api.activate_managed_runtime',
                        side_effect=ResourceDeferred('learning_prepare_memory_pressure')):
            code, row = self.run_cli('activate-learning-runtime',
                                     '--learning-runtime-executable', '/tmp/venv/bin/python',
                                     '--learning-model-dir', '/tmp/model',
                                     '--approve-learning-runtime-activation')
        self.assertEqual((code, row['status'], row['changesApplied']), (75, 'deferred', 0))
        self.assertEqual(row['reason'], 'learning_prepare_memory_pressure')
