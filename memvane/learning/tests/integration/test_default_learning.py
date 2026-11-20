"""Default enrollment respects explicit preferences and survives missing runtime."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


class DefaultLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir(mode=0o700)
        (self.project / '.memvane').mkdir(mode=0o700)
        self.settings = self.project / '.memvane' / 'settings.json'
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.write({'name': 'fixture'})

    def write(self, value):
        self.settings.write_text(json.dumps(value), encoding='utf8')
        self.settings.chmod(0o600)

    def test_missing_runtime_records_one_choice_and_is_idempotent(self):
        from memvane.learning import default_enrollment
        first = default_enrollment.record(self.project, self.state, 'abc123')
        before = (self.state / default_enrollment.NAME).read_bytes()
        second = default_enrollment.record(self.project, self.state, 'abc123')
        self.assertEqual(first['status'], 'pending_runtime')
        self.assertEqual(second['status'], 'pending_runtime')
        self.assertEqual(before, (self.state / default_enrollment.NAME).read_bytes())
        self.assertEqual(first['choice'], 'local-auto-apply')
        row = json.loads(before)
        self.assertNotEqual(row['namespaceRoot'], str(default_enrollment.namespace(self.project)))
        self.assertEqual(row['namespaceRoot'], str(default_enrollment.default_namespace(self.project, 'abc123')))

    def test_missing_runtime_does_not_prepare_or_activate(self):
        from memvane.learning import default_enrollment
        from memvane.learning.context_learning.selector_heal import HealRefused
        with mock.patch('memvane.learning.context_learning.selector_heal.official_runtime',
                        side_effect=HealRefused('learning_runtime_not_installed')), \
             mock.patch.object(default_enrollment, '_prepare_namespace') as prepare, \
             mock.patch.object(default_enrollment, '_activate_profile') as activate:
            result = default_enrollment.prepare(self.project, self.state, 'abc123', lease_fd=123)
        self.assertEqual(result['status'], 'pending_runtime')
        self.assertTrue((self.state / default_enrollment.NAME).is_file())
        prepare.assert_not_called()
        activate.assert_not_called()

    def test_opt_out_after_intent_prevents_preparation(self):
        from memvane.learning import default_enrollment
        default_enrollment.record(self.project, self.state, 'abc123')
        self.write({'name': 'fixture', 'contextLearning': {'capture': False}})
        with mock.patch('memvane.learning.context_learning.selector_heal.official_runtime') as runtime:
            result = default_enrollment.prepare(self.project, self.state, 'abc123', lease_fd=123)
        self.assertEqual(result['status'], 'disabled')
        runtime.assert_not_called()

    def test_project_inventory_requires_the_executing_enabled_codex_install(self):
        from memvane.learning.context_learning import project_scope
        home = self.root / 'home'
        root = home / '.codex/plugins/cache/market/memvane/1.0.7'
        adapter = root / 'memvane/learning/context_learning/adapter.py'
        adapter.parent.mkdir(mode=0o700, parents=True)
        adapter.touch(mode=0o600)
        entry = {'name': 'memvane', 'installed': True, 'enabled': True,
                 'marketplaceName': 'market', 'version': '1.0.7'}
        with mock.patch('pathlib.Path.home', return_value=home), \
             mock.patch('memvane.learning.context_learning.selector_heal.running_adapter', return_value=adapter), \
             mock.patch('memvane.learning.context_learning.host_inventory._project_listing', return_value={'installed': [entry]}) as listing:
            self.assertEqual(project_scope._local_installations(self.project), [{'host': 'codex', 'root': str(root)}])
            listing.return_value = {'installed': [{**entry, 'enabled': False}]}
            with self.assertRaisesRegex(ValueError, 'project_scope_installation_invalid'):
                project_scope._local_installations(self.project)

    def test_any_explicit_off_prevents_default_enrollment(self):
        from memvane.learning import default_enrollment
        for block in ({'contextLearning': {'capture': False}},
                      {'contextLearning': {'autoCycle': False}},
                      {'sharedLearning': {'enabled': False}}):
            with self.subTest(block=block):
                self.write({'name': 'fixture', **block})
                self.assertEqual(default_enrollment.record(self.project, self.state, 'abc123')['status'], 'disabled')
                self.assertFalse((self.state / default_enrollment.NAME).exists())

    def test_malformed_learning_settings_stay_disabled(self):
        from memvane.learning import default_enrollment
        for block in ({'contextLearning': 'bad'}, {'sharedLearning': {'enabled': 'yes'}}):
            with self.subTest(block=block):
                self.write({'name': 'fixture', **block})
                self.assertEqual(default_enrollment.record(self.project, self.state, 'abc123')['status'], 'disabled')

    def test_existing_profile_is_preserved(self):
        from memvane.learning import default_enrollment
        existing = self.root / 'existing'
        existing.mkdir(mode=0o700)
        profile = existing / 'learning-profile.json'
        profile.write_text('{"existing":true}', encoding='utf8')
        profile.chmod(0o600)
        self.write({'name': 'fixture', 'sharedLearning': {'root': str(existing)}})
        self.assertEqual(default_enrollment.record(self.project, self.state, 'abc123')['status'], 'preserved')
        self.assertEqual(profile.read_text(encoding='utf8'), '{"existing":true}')
        self.assertFalse((self.state / default_enrollment.NAME).exists())

    def test_explicit_no_auto_apply_is_preserved(self):
        from memvane.learning import default_enrollment
        self.write({'name': 'fixture', 'sharedLearning': {'autoApply': False}})
        self.assertEqual(default_enrollment.record(self.project, self.state, 'abc123')['status'], 'preserved')
        self.assertFalse((self.state / default_enrollment.NAME).exists())

    def test_default_setup_does_not_reverse_late_opt_out(self):
        from memvane.learning import project_learning_setup as setup
        for block in ({'contextLearning': {'capture': False}},
                      {'contextLearning': {'autoCycle': False}},
                      {'sharedLearning': {'enabled': False}},
                      {'sharedLearning': {'autoApply': False}}):
            with self.subTest(block=block):
                with self.assertRaisesRegex(ValueError, 'setup_learning_disabled'):
                    setup._desired_settings({'name': 'fixture', **block}, self.state, mode=setup.DEFAULT_LOCAL)

    def test_preparation_precedes_heal_and_warm_after_runtime_ready(self):
        from memvane.learning import default_enrollment
        default_enrollment.record(self.project, self.state, 'abc123')
        calls = []
        target = Path(json.loads((self.state / default_enrollment.NAME).read_text())['namespaceRoot'])
        def prepare_root(*args):
            target.mkdir(mode=0o700, parents=True)
            calls.append('prepare')
        with mock.patch.object(default_enrollment, '_prepare_namespace', side_effect=prepare_root), \
             mock.patch.object(default_enrollment, '_activate_profile', create=True, side_effect=lambda *a: calls.append('profile')), \
             mock.patch('memvane.learning.context_learning.selector_heal.official_runtime', return_value=('/fake/python', '/fake/model')), \
             mock.patch('memvane.learning.context_learning.selector_heal.pressure', return_value=None), \
             mock.patch('memvane.learning.context_learning.selector_heal.heal', side_effect=lambda *a, **k: calls.append('heal') or {'status': 'selector_prepared'}), \
             mock.patch('memvane.learning.context_learning.selector_heal.warm', side_effect=lambda *a, **k: calls.append('warm') or {'status': 'selector_warm'}):
            result = default_enrollment.prepare(self.project, self.state, 'abc123', lease_fd=123)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(calls, ['prepare', 'heal', 'warm', 'profile'])

    def test_namespace_creates_private_base_without_copying_model(self):
        from memvane.learning import default_enrollment
        from memvane.learning.context_learning import adapter
        model = self.root / 'model'
        model.mkdir(mode=0o700)
        for name in adapter.MODEL_FILES:
            path = model / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(name, encoding='utf8')
        support = self.root / 'support'
        support.mkdir(mode=0o700)
        target = support / 'v1' / 'live-selection' / 'namespace'
        with mock.patch('memvane.learning.context_learning.runtime_setup.learning_state_base', return_value=support / 'v1'):
            default_enrollment._prepare_namespace(target, '/runtime/python', str(model))
            default_enrollment._prepare_namespace(target, '/runtime/python', str(model))
        pointer = json.loads((target / 'active-checkpoint.json').read_text())
        self.assertEqual(pointer['model_input_view'], adapter.EDIT_INPUT_VIEW)
        self.assertEqual(pointer['artifact_path'], str(target / 'base.artifact'))
        self.assertEqual((target.stat().st_mode & 0o777), 0o700)

    def test_default_grant_excludes_gold_and_allows_long_expiry(self):
        from memvane.learning.context_learning import project_scope
        now = int(time.time() * 1000)
        with mock.patch('memvane.learning.default_enrollment.authorized', create=True, return_value=True), \
             mock.patch.object(project_scope, '_local_installations', create=True, return_value=[]), \
             mock.patch.object(project_scope, '_binding', return_value={'fixture': True}):
            grant = project_scope.proposal(self.project, self.state, {},
                expires_at_unix_ms=now + 365 * 86400000,
                not_before_unix_ms=now - 1000, local_only=True)
        self.assertEqual(grant['operations'], ['local_train', 'quality_evaluate'])

    def test_authorization_requires_selected_install_and_matching_intent(self):
        from memvane.learning import default_enrollment
        default_enrollment.record(self.project, self.state, 'abc123')
        intent = json.loads((self.state / default_enrollment.NAME).read_text())
        root = Path(intent['namespaceRoot'])
        root.mkdir(mode=0o700, parents=True)
        binding = root / default_enrollment.BINDING
        binding.write_text(json.dumps(intent), encoding='utf8')
        binding.chmod(0o600)
        decision = {'installId': 'abc123', 'journalPath': str(self.state / 'journal')}
        with mock.patch('memvane.journal.api.select_route', return_value=decision), \
             mock.patch('memvane.journal.api.verify_selected_route_for_write', return_value={'installId': 'abc123'}):
            self.assertTrue(default_enrollment.authorized(self.project, root))
            intent['installId'] = 'other'
            binding.write_text(json.dumps(intent), encoding='utf8')
            self.assertFalse(default_enrollment.authorized(self.project, root))

    def test_default_profile_transaction_creates_default_teacher_grant(self):
        self.profile_transaction(teacher_off=False)

    def test_default_profile_transaction_with_teacher_off_creates_only_local_grant(self):
        self.profile_transaction(teacher_off=True)

    def teacher_off_switch(self, root):
        from memvane.learning.context_learning import teacher_state
        for directory in (root.parent.parent, root.parent, root):
            directory.mkdir(mode=0o700, exist_ok=True)
        self.assertTrue(teacher_state.write_switch(root, False))

    def profile_transaction(self, *, teacher_off):
        from memvane.learning import default_enrollment, project_learning_setup
        from memvane.learning.context_learning import adapter, project_scope, shared
        model = self.root / 'model'
        model.mkdir(mode=0o700)
        for name in adapter.MODEL_FILES:
            path = model / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else 'fixture', encoding='utf8')
            path.chmod(0o600)
        (model / 'rl_agent_config.json').write_text(json.dumps({'encoder':'jhu-clsp/mmBERT-base','max_len':1024,'head_max_len':256}))
        runtime = self.root / 'python'
        runtime.write_text('fixture runtime')
        runtime.chmod(0o700)
        support = self.root / 'support'
        support.mkdir(mode=0o700)
        root = support / 'v1' / 'live-selection' / 'namespace'
        source = Path(adapter.__file__).resolve().parents[3]
        installs = [{'host': 'codex', 'root': str(source)}]
        with mock.patch('memvane.learning.context_learning.runtime_setup.learning_state_base', return_value=support / 'v1'), \
             mock.patch.object(default_enrollment, 'authorized', return_value=True), \
             mock.patch.object(project_scope, '_local_installations', create=True, return_value=installs), \
             mock.patch.object(project_scope, '_installed_binding', return_value=installs), \
             mock.patch.object(project_scope, '_binding', return_value={'installations': installs}):
            default_enrollment._prepare_namespace(root, str(runtime), str(model))
            if teacher_off:
                self.teacher_off_switch(root)
            default_enrollment._activate_profile(self.project, root)
            grant = json.loads((root / project_scope.NAME).read_text())['proposal']
            profile = json.loads((root / 'learning-profile.json').read_text())['proposal']
            policy = shared._policy(root)
            self.assertEqual(default_enrollment.default_local_only(root, self.project, policy), teacher_off)
            self.assertEqual(default_enrollment.default_teacher(root, self.project, policy), not teacher_off)
            with self.assertRaisesRegex(ValueError, 'operation_invalid'):
                project_scope.require(root, policy, 'gold')
            if teacher_off:
                with self.assertRaisesRegex(ValueError, 'operation_invalid'):
                    project_scope.require(root, policy, 'gold_default')
            else:
                self.assertEqual(project_scope.require(root, policy, 'gold_default'), grant)
        self.assertEqual(grant['operations'], project_scope.LOCAL_OPERATIONS if teacher_off
                         else project_scope.DEFAULT_TEACHER_OPERATIONS)
        self.assertEqual(profile['choice'], 'local-auto-apply')
        self.assertEqual(profile['mode'], 'new')
        self.assertTrue(profile['autoApply'])
        self.assertEqual((root / 'project-teacher-consent.json').exists(), not teacher_off)

    def test_pending_choice_creates_one_profile_and_grant_after_warm(self):
        self.pending_choice(teacher_off=False)

    def test_pending_choice_with_teacher_off_creates_local_grant_after_warm(self):
        self.pending_choice(teacher_off=True)

    def pending_choice(self, *, teacher_off):
        from memvane.learning import default_enrollment
        from memvane.learning.context_learning import adapter, project_scope
        model = self.root / 'model'
        model.mkdir(mode=0o700)
        for name in adapter.MODEL_FILES:
            path = model / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('.json') else 'fixture', encoding='utf8')
            path.chmod(0o600)
        (model / 'rl_agent_config.json').write_text(json.dumps({'encoder': 'jhu-clsp/mmBERT-base', 'max_len': 1024, 'head_max_len': 256}))
        runtime = self.root / 'python'
        runtime.write_text('fixture runtime')
        runtime.chmod(0o700)
        support = self.root / 'support'
        support.mkdir(mode=0o700)
        installs = [{'host': 'codex', 'root': str(Path(adapter.__file__).resolve().parents[3])}]
        events = []
        with mock.patch('memvane.learning.context_learning.runtime_setup.learning_state_base', return_value=support / 'v1'), \
             mock.patch.object(default_enrollment, 'authorized', return_value=True), \
             mock.patch.object(project_scope, '_local_installations', return_value=installs), \
             mock.patch.object(project_scope, '_installed_binding', return_value=installs), \
             mock.patch.object(project_scope, '_binding', return_value={'installations': installs}), \
             mock.patch('memvane.learning.context_learning.selector_heal.official_runtime', return_value=(str(runtime), str(model))), \
             mock.patch('memvane.learning.context_learning.selector_heal.pressure', return_value=None), \
             mock.patch('memvane.learning.context_learning.selector_heal.heal', side_effect=lambda *a, **k: events.append('heal') or {'status': 'selector_prepared'}), \
             mock.patch('memvane.learning.context_learning.selector_heal.warm', side_effect=lambda *a, **k: events.append('warm') or {'status': 'selector_warm'}):
            root = default_enrollment.default_namespace(self.project, 'abc123')
            if teacher_off:
                self.teacher_off_switch(root)
            result = default_enrollment.prepare(self.project, self.state, 'abc123', lease_fd=123)
            profile = json.loads((root / 'learning-profile.json').read_text())['proposal']
            grant = json.loads((root / project_scope.NAME).read_text())['proposal']
            self.assertEqual(default_enrollment.prepare(self.project, self.state, 'abc123', lease_fd=123)['status'], 'preserved')
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(events, ['heal', 'warm'])
        self.assertEqual(profile['choice'], 'local-auto-apply')
        self.assertEqual(profile['mode'], 'new')
        self.assertEqual(grant['operations'], project_scope.LOCAL_OPERATIONS if teacher_off
                         else project_scope.DEFAULT_TEACHER_OPERATIONS)
        self.assertEqual((root / 'project-teacher-consent.json').exists(), not teacher_off)
