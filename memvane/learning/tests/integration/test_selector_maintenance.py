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
    def test_spawn_failure_is_visible_when_request_timestamp_was_written(self):
        decision = {'projectRoot': '/project', 'storePath': '/store',
                    'journalPath': '/state/journal', 'installId': 'test'}
        with mock.patch('memvane.runtime.api.validate_sync_owner'), \
             mock.patch('memvane.journal.api.inspect_journal', return_value={'state': 'selected', 'installId': 'test', 'maxPluginVersion': '1.0.0'}), \
             mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.1'), \
             mock.patch('memvane.learning.default_enrollment.record', return_value={'status': 'pending_runtime'}), \
             mock.patch('memvane.journal.api.ledger_read_json', side_effect=FileNotFoundError), \
             mock.patch('memvane.journal.api.ledger_write'), \
             mock.patch('memvane.learning.selector_maintenance.subprocess.Popen', side_effect=OSError('fixture')), \
             mock.patch('memvane.diagnostics.api.emit') as emit:
            with self.assertRaises(OSError):
                selector_maintenance._request_owned(decision, object(), force=False)
        self.assertTrue(any(c.args[0] == 'learning.selector.request' and c.kwargs.get('outcome') == 'failed' for c in emit.call_args_list))

    def test_entry_records_selected_proof_failure(self):
        with tempfile.TemporaryDirectory(prefix='mvs-') as directory:
            root = Path(directory).resolve()
            decision = {'projectRoot': str(root), 'journalPath': str(root/'journal'), 'installId': 'test'}
            with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                 mock.patch('memvane.journal.api.verify_selected_route_for_write', side_effect=ValueError('proof_failed')), \
                 mock.patch('memvane.diagnostics.api.emit') as emit:
                with self.assertRaises(ValueError):
                    selector_maintenance.run(root, force=True)
            outcomes = [(c.args[0], c.kwargs.get('outcome'), c.kwargs.get('reason')) for c in emit.call_args_list]
            self.assertIn(('learning.selector.maintenance', 'started', None), outcomes)
            self.assertIn(('learning.selector.maintenance', 'failed', 'selected_proof_unavailable'), outcomes)

    def test_subprocess_entry_runs_pending_activation_through_real_installer(self):
        import json
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(prefix='mvs-entry-') as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            runtime = root / 'runtime'; runtime.mkdir(mode=0o700)
            generation = runtime / 'generations' / 'g-fixture'
            generation.mkdir(mode=0o700, parents=True)
            (runtime / 'generations').chmod(0o700)
            executable = generation / 'venv' / 'bin' / 'python'
            executable.parent.mkdir(parents=True)
            executable.write_text('fixture', encoding='utf8'); executable.chmod(0o700)
            identity, model_hash = 'a' * 64, 'b' * 64
            prepared = {'schema_version': 1, 'executable': str(executable),
                        'runtime_identity_sha256': identity, 'uv_sha256': 'c' * 64, 'lock_sha256': 'd' * 64}
            (generation / 'prepared.json').write_text(json.dumps(prepared)); (generation / 'prepared.json').chmod(0o600)
            model = root / 'model'; model.mkdir(mode=0o700)
            pending = {'schema': 'memvane-learning-pending-v1', 'executable': str(executable),
                       'model_dir': str(model), 'model_identity': model_hash, 'runtime_identity_sha256': identity}
            pending_path = runtime / 'pending-activation.json'
            pending_path.write_text(json.dumps(pending)); pending_path.chmod(0o600)
            entry = Path(__file__).resolve().parents[4] / 'entry' / 'run.py'
            script = r'''
import os, runpy, sys
from pathlib import Path
from unittest import mock
from memvane.learning import runtime_activation as activation
from memvane.learning.context_learning import runtime_installer as installer
root, entry = Path(sys.argv[1]), sys.argv[2]
runtime = root / 'runtime'
decision = {'projectRoot': str(root), 'storePath': str(root/'store.sqlite'),
            'journalPath': str(root/'journal.sqlite'), 'installId': 'test'}
real = installer.activate_generation
seen = []
def invoke(*args, **kwargs):
    def probe(*a, **k):
        seen.append('probe')
        return {'status': 'metadata_compatible', 'runtime_identity_sha256': 'a'*64}
    def prove(*a):
        seen.append('prove')
        return {'probe': {'runtime_identity_sha256':'a'*64},
                'attestation': {'base_model_sha256':'b'*64, 'adapter_sha256':'c'*64}}
    return real(*args, **kwargs, probe=probe, prover=prove)
def emit(event, **kwargs):
    if event == 'learning.runtime.proof' and kwargs.get('outcome') == 'accepted': seen.append('accepted')
with mock.patch.object(activation, '_root', return_value=runtime), \
     mock.patch.object(activation, '_memory_admission', return_value=True), \
     mock.patch.object(installer, 'activate_generation', side_effect=invoke), \
     mock.patch.object(installer, 'verify_compatibility', return_value=True), \
     mock.patch('memvane.learning.context_learning.adapter.model_identity', return_value='b'*64), \
     mock.patch('memvane.learning.context_learning.selector_heal._prepare_lease', side_effect=lambda *a: os.open(os.devnull, os.O_RDONLY)), \
     mock.patch('memvane.journal.api.select_route', return_value=decision), \
     mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value={'installId':'test'}), \
     mock.patch('memvane.journal.api.inspect_journal', return_value={'maxPluginVersion':'1.0.0'}), \
     mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.1'), \
     mock.patch('memvane.learning.default_enrollment.prepare', return_value={'status':'pending_runtime'}), \
     mock.patch('memvane.diagnostics.api.emit', side_effect=emit):
    sys.argv = [entry, 'selector-maintenance', str(root), 'force']
    try: runpy.run_path(entry, run_name='__main__')
    except SystemExit as exc:
        if exc.code: raise
assert seen == ['probe', 'prove', 'accepted'], seen
'''
            result = subprocess.run([sys.executable, '-B', '-c', script, str(root), str(entry)],
                                    cwd=str(entry.parents[1]), text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(pending_path.exists())
            self.assertEqual(json.loads((runtime / 'active.json').read_text())['runtime_identity_sha256'], identity)

    def test_entry_continues_existing_selector_after_pending_deferral(self):
        import runpy
        import sys
        from memvane.kernel.resource_deferred import ResourceDeferred
        with tempfile.TemporaryDirectory(prefix='mvs-') as directory:
            root = Path(directory).resolve()
            decision = {'projectRoot': str(root), 'storePath': str(root / 'store.sqlite'),
                        'journalPath': str(root / 'journal.sqlite'), 'installId': 'test'}
            events = []
            fd = os.open(os.devnull, os.O_RDONLY)
            entry = Path(__file__).resolve().parents[4] / 'entry' / 'run.py'
            with mock.patch.object(sys, 'argv', [str(entry), 'selector-maintenance', str(root), 'force']), \
                 mock.patch('memvane.journal.api.select_route', return_value=decision), \
                 mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value={'installId': 'test'}), \
                 mock.patch('memvane.learning.runtime_activation.ensure_pending', side_effect=ResourceDeferred('learning_prepare_memory_pressure')), \
                 mock.patch('memvane.journal.api.inspect_journal', return_value={'maxPluginVersion': '1.0.0'}), \
                 mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.1'), \
                 mock.patch.object(selector_heal, '_prepare_lease', return_value=fd), \
                 mock.patch('memvane.learning.default_enrollment.prepare', return_value={'status': 'preserved'}), \
                 mock.patch.object(selector_maintenance, '_selector_state', return_value=root), \
                 mock.patch.object(selector_heal, 'heal', side_effect=lambda *a, **k: events.append('heal') or {'status': 'selector_ready'}), \
                 mock.patch.object(selector_heal, 'warm', side_effect=lambda *a, **k: events.append('warm') or {'status': 'selector_warm'}), \
                 mock.patch('memvane.runtime.api.sync_owner', return_value=nullcontext(object())), \
                 mock.patch('memvane.journal.api.publish_plugin_version', side_effect=lambda *a: events.append('publish')), \
                 mock.patch('memvane.diagnostics.api.emit') as emit:
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(entry), run_name='__main__')
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(events, ['heal', 'warm', 'publish'])
            self.assertTrue(any(c.args[0] == 'learning.runtime.proof' and c.kwargs.get('outcome') == 'deferred' for c in emit.call_args_list))

    def test_deferred_default_preparation_does_not_heal_a_different_namespace(self):
        with tempfile.TemporaryDirectory(prefix='mvs-') as directory:
            root = Path(directory).resolve()
            decision = {'projectRoot': str(root), 'storePath': str(root / 'store.sqlite'),
                        'journalPath': str(root / 'journal.sqlite'), 'installId': 'test'}
            fd = os.open(os.devnull, os.O_RDONLY)
            with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                 mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value={'installId': 'test'}), \
                 mock.patch('memvane.learning.runtime_activation.ensure_pending'), \
                 mock.patch('memvane.journal.api.inspect_journal', return_value={'maxPluginVersion': '1.0.0'}), \
                 mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.1'), \
                 mock.patch.object(selector_heal, '_prepare_lease', return_value=fd), \
                 mock.patch('memvane.learning.default_enrollment.prepare', return_value={'status': 'selector_warm_busy'}), \
                 mock.patch.object(selector_heal, 'heal') as heal, \
                 mock.patch('memvane.journal.api.publish_plugin_version') as publish:
                selector_maintenance.run(root)
            heal.assert_not_called()
            publish.assert_not_called()

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
