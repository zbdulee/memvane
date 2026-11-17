"""Shared project settings reach the real warm/profile/grant entry path."""
from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import runpy
import stat
import sys
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment
from memvane.learning.context_learning import adapter, dataset, inference_service
from memvane.learning.context_learning import learning_profile, project_scope, selector_heal, shared


class SettingsProfile(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        folder = self.project / '.memvane'
        folder.mkdir(mode=0o755, parents=True)
        self.settings = folder / 'settings.json'
        self.settings.write_text('{"name":"fixture"}', encoding='utf8')
        self.settings.chmod(0o644)

    def test_scope_reads_shared_settings_without_changing_bytes_or_mode(self):
        before = self.settings.read_bytes()
        self.assertTrue(project_scope._settings_scope(self.project)['indexing'])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o644)

    def test_unsafe_settings_and_private_state_stay_rejected(self):
        for mode in (0o664, 0o666):
            self.settings.chmod(mode)
            with self.assertRaises(ValueError):
                project_scope._settings_scope(self.project)
        self.settings.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe_input_file'):
            dataset.read_private_json(self.settings)
        with mock.patch.object(learning_profile.os, 'getuid', return_value=os.getuid()+1):
            with self.assertRaises(ValueError):
                project_scope._settings_scope(self.project)
        alias = self.settings.with_name('hardlink.json')
        os.link(self.settings, alias)
        with self.assertRaises(ValueError):
            project_scope._settings_scope(self.project)
        alias.unlink()
        self.settings.write_bytes(b' ' * (1024*1024+1))
        with self.assertRaises(ValueError):
            project_scope._settings_scope(self.project)

    def test_symlink_settings_are_rejected(self):
        target = self.settings.with_name('target.json')
        self.settings.rename(target)
        self.settings.symlink_to(target)
        with self.assertRaises(ValueError):
            project_scope._settings_scope(self.project)

    def test_entry_warms_then_creates_profile_and_local_grant_from_0644_settings(self):
        self.run_profile_entry()

    def run_profile_entry(self, *, seed_namespace=None, heal_impl=None, expected_error=None):
        model = self.home / 'model'
        model.mkdir(mode=0o700)
        for name in adapter.MODEL_FILES:
            path = model / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else 'synthetic model', encoding='utf8')
            path.chmod(0o600)
        (model / 'rl_agent_config.json').write_text(json.dumps({
            'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024, 'head_max_len': 256}), encoding='utf8')
        runtime = self.home / 'python'
        runtime.write_text('synthetic runtime', encoding='utf8')
        runtime.chmod(0o700)
        state = self.home / 'install'
        state.mkdir(mode=0o700)
        store = state / 'store.sqlite'
        store.touch(mode=0o600)
        decision = {'projectRoot': str(self.project), 'journalPath': str(state / 'journal'),
                    'storePath': str(store), 'installId': 'fixture', 'state': 'candidate'}
        root = default_enrollment.default_namespace(self.project, 'fixture')
        root.parent.parent.mkdir(mode=0o700, parents=True)
        source = Path(adapter.__file__).resolve().parents[3]
        if seed_namespace is not None:
            seed_namespace(root, str(runtime), str(model), source)
        installs = [{'host': 'codex', 'root': str(source)}]
        events = []
        original_activate = default_enrollment._activate_profile
        def activate(project, namespace):
            self.assertTrue((namespace / selector_heal.WARM_MARKER).is_file())
            self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o644)
            events.append('profile')
            return original_activate(project, namespace)
        def policy(namespace, *_args, **_kwargs):
            value = dataset.read_private_json(namespace / selector_heal.POLICY)
            return value, value
        def exchange(_root, envelope, _deadline):
            self.assertEqual(envelope['request']['cases'][0]['request_id'], 'selector-warm')
            events.append('warm')
            return {'status': 'ok', 'prediction': {}, 'timing': {'forward_seconds': .001}}
        entry = source / 'entry/run.py'
        stderr = io.StringIO()
        projections = ExitStack()
        self.addCleanup(projections.close)
        projections.enter_context(mock.patch('memvane.learning.context_learning.host_self.selection',
            return_value=(installs, {'schema':'synthetic-host-self'})))
        with mock.patch('memvane.journal.api.select_route', return_value=decision), \
                mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value={'installId': 'fixture'}), \
                mock.patch('memvane.journal.api.inspect_journal', return_value={
                    'state': 'selected', 'installId': 'fixture', 'maxPluginVersion': '1.0.12'}), \
                mock.patch('memvane.journal.api.publish_plugin_version'), \
                mock.patch('memvane.learning.runtime_activation.ensure_pending'), \
                mock.patch.object(project_scope, '_local_installations', return_value=installs), \
                mock.patch.object(project_scope, '_installed_binding', return_value=installs), \
                mock.patch.object(selector_heal, 'official_runtime', return_value=(str(runtime), str(model))), \
                mock.patch.object(selector_heal, 'pressure', return_value=None), \
                mock.patch.object(selector_heal, 'heal', side_effect=heal_impl, return_value={'status': 'selector_ready'}), \
                mock.patch.object(selector_heal, '_service_identity', return_value=[1, 2, 3, 'synthetic']), \
                mock.patch.object(inference_service, '_SERVICE_STARTER', None), \
                mock.patch.object(inference_service, '_policy', side_effect=policy), \
                mock.patch.object(inference_service, '_ensure'), \
                mock.patch.object(inference_service, '_exchange', side_effect=exchange), \
                mock.patch.object(default_enrollment, '_activate_profile', side_effect=activate), \
                mock.patch('memvane.diagnostics.api.emit'), \
                mock.patch.object(sys, 'argv', [str(entry), 'selector-maintenance', str(self.project), 'force']), \
                redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ended:
                runpy.run_path(str(entry), run_name='__main__')
            self.assertEqual(ended.exception.code, 0)
            if expected_error is not None:
                self.assertIn(expected_error, stderr.getvalue())
                self.assertFalse((root / selector_heal.WARM_MARKER).exists())
                self.assertFalse((root / learning_profile.NAME).exists())
                self.assertFalse((root / project_scope.NAME).exists())
                return root
            grant_path = root / project_scope.NAME
            self.assertTrue(grant_path.is_file(), stderr.getvalue()[-1000:])
            grant = dataset.read_private_json(grant_path)['proposal']
            self.assertTrue((root / learning_profile.NAME).is_file(), stderr.getvalue()[-4000:])
            profile = dataset.read_private_json(root / learning_profile.NAME)['proposal']
            self.assertEqual(grant['operations'], ['local_train', 'quality_evaluate'])
            self.assertEqual(profile['choice'], 'local-auto-apply')
            self.assertTrue(project_scope.require(root, shared._policy(root), 'local_train'))
            grant_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'unsafe|private'):
                project_scope.require(root, shared._policy(root), 'local_train')
            grant_path.chmod(0o600)
        self.assertEqual(events, ['warm', 'profile'])
        return root


if __name__ == '__main__':
    import unittest
    unittest.main()
