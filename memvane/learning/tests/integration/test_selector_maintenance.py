"""Selector maintenance preserves heal, warm, then version publication order."""
from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memvane.learning import selector_maintenance
from memvane.learning.context_learning import runtime_installer, selector_heal


class SelectorMaintenance(unittest.TestCase):
    def test_session_start_retries_default_enrollment_for_same_version(self):
        decision = {'projectRoot': '/project', 'storePath': '/store',
                    'journalPath': '/state/journal', 'installId': 'test'}
        with mock.patch('memvane.runtime.api.validate_sync_owner'), \
             mock.patch('memvane.journal.api.inspect_journal', return_value={'state': 'selected', 'installId': 'test', 'maxPluginVersion': '1.0.3'}), \
             mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.3'), \
             mock.patch('memvane.learning.default_enrollment.record', return_value={'status': 'pending_runtime'}), \
             mock.patch('memvane.learning.selector_maintenance._missing_warm_receipt', return_value=False), \
             mock.patch('memvane.journal.api.ledger_read_json', side_effect=FileNotFoundError), \
             mock.patch('memvane.journal.api.ledger_write'), \
             mock.patch('memvane.learning.selector_maintenance.subprocess.Popen') as spawn:
            selector_maintenance._request_owned(decision, object(), force=False)
            self.assertEqual(spawn.call_count, 1)

    def test_newer_version_heals_warms_and_then_publishes_once(self):
        with tempfile.TemporaryDirectory(prefix='mvs-') as directory:
            root = Path(directory).resolve()
            decision = {'projectRoot': str(root), 'storePath': str(root / 'store.sqlite'),
                        'journalPath': str(root / 'journal.sqlite'), 'installId': 'test'}
            proof = {'installId': 'test'}
            calls = []
            fd = os.open(os.devnull, os.O_RDONLY)
            with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                 mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value=proof), \
                 mock.patch('memvane.journal.api.inspect_journal', return_value={'maxPluginVersion': '1.0.0'}), \
                 mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.1'), \
                 mock.patch.object(selector_heal, '_prepare_lease', return_value=fd), \
                 mock.patch('memvane.learning.default_enrollment.prepare', return_value={'status': 'preserved'}), \
                 mock.patch.object(selector_maintenance, '_selector_state', return_value=root), \
                 mock.patch.object(selector_heal, 'heal', side_effect=lambda *a, **k: calls.append('heal') or {'status': 'selector_ready'}), \
                 mock.patch.object(selector_heal, 'warm', side_effect=lambda *a, **k: calls.append('warm') or {'status': 'selector_warm'}), \
                 mock.patch('memvane.runtime.api.sync_owner', return_value=nullcontext(object())), \
                 mock.patch('memvane.journal.api.publish_plugin_version', side_effect=lambda *a: calls.append('publish')):
                selector_maintenance.run(root)
            self.assertEqual(calls, ['heal', 'warm', 'publish'])

    def test_installer_reads_the_single_execution_version_source(self):
        with mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.2.3') as version:
            self.assertEqual(runtime_installer._installed_plugin_version(), '1.2.3')
        version.assert_called_once_with()
        with mock.patch('memvane.kernel.memvane_version.plugin_version', side_effect=ValueError('invalid_plugin_version')):
            self.assertIsNone(runtime_installer._installed_plugin_version())
