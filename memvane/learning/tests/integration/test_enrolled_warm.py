"""Real enrollment pointers meet the unchanged warm/request/selector readers."""
import hashlib
import json
from pathlib import Path
import sys
import time
from contextlib import ExitStack
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import default_enrollment
from memvane.learning.context_learning import adapter, inference_identity, inference_service
from memvane.learning.context_learning import live_select, selector_heal


class EnrolledWarm(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        (self.project / '.memvane').mkdir(mode=0o700, parents=True)
        self.write(self.project / '.memvane/settings.json', {'name': 'fixture'})
        self.install = self.home / 'install'
        self.install.mkdir(mode=0o700)
        self.model = self.home / 'model'
        self.model.mkdir(mode=0o700)
        for name in adapter.MODEL_FILES:
            target = self.model / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text('{}' if name.endswith('.json') else 'synthetic model bytes')
            target.chmod(0o600)
        self.state = default_enrollment.default_namespace(self.project, 'fixture')
        self.state.parent.parent.mkdir(mode=0o700, parents=True)
        self.argv = [sys.executable, str(selector_heal.running_adapter()), str(self.model)]
        self.verified = {'runtime_identity_sha256': 'r' * 64, 'base_model_sha256': 'm' * 64,
                         'adapter_sha256': inference_identity.file_sha(self.argv[1])}
        self.seen = []

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding='utf8')
        path.chmod(0o600)

    def namespace(self):
        default_enrollment._prepare_namespace(self.state, sys.executable, str(self.model))
        return json.loads((self.state / 'active-checkpoint.json').read_text())

    def exchange(self, _root, envelope, _deadline):
        checked = inference_service._validate_request(envelope)
        self.seen.append(checked)
        case = checked['request']['cases'][0]
        return {'status': 'ok', 'timing': {}, 'prediction': {'schema_version': 1, 'predictions': [{
            'request_id': case['request_id'], 'input_sha256': case['input_sha256'],
            'selected_ids': [case['candidates'][0]['id']]}]}}

    def transport(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(inference_service, '_ensure'))
        stack.enter_context(mock.patch.object(inference_service, '_exchange', side_effect=self.exchange))
        stack.enter_context(mock.patch.object(selector_heal, '_service_identity', return_value=[1, 2, 3, 'fixture']))
        stack.enter_context(mock.patch('memvane.learning.context_learning.local_cycle.VerifiedLayaTrainer.validate_prediction',
                                       return_value=self.verified))
        self.verify = stack.enter_context(
            mock.patch.object(inference_service._IDENTITY_CACHE, 'verify', return_value=self.verified))

    def test_enrollment_pointer_passes_real_warm_validator_and_live_selector(self):
        active = self.namespace()
        self.transport()
        # With inference evidence the worker validates through the identity cache.
        with mock.patch.object(inference_service, 'has_evidence', return_value=True):
            self.assertEqual(selector_heal.warm(self.project, state=self.state)['status'], 'selector_warm')
        # The warm-up request names no lane: its validation is swept every time.
        self.assertEqual([call.kwargs for call in self.verify.call_args_list], [{'reuse': False}])
        body = '---\ntitle: Cobalt\n---\n# Cobalt\nThe synthetic marker is cobalt.\n'
        selected, reason, _ = live_select.choose(self.state, 'Which marker?', [
            {'id': 'wiki/cobalt.md', 'revision_sha256': hashlib.sha256(body.encode()).hexdigest(),
             'source_text': body}], corpus_fingerprint='f' * 64)
        self.assertEqual((selected, reason), (['wiki/cobalt.md'], 'selected'))
        self.assertEqual(active['schema_version'], 1)
        self.assertEqual(len(self.seen), 2)

    def record_receipt(self, *_args):
        return inference_identity._record(self.state, self.argv, view=adapter.EDIT_INPUT_VIEW,
                                          verified=self.verified)

    def test_old_pointer_is_rewritten_reprepared_and_warmed_in_one_prepare(self):
        active = self.namespace()
        active.pop('schema_version', None)
        self.write(self.state / 'active-checkpoint.json', active)
        self.write(self.state / 'laya-synthetic-attestation.json',
                   {'attestation': {'adapter_sha256': self.verified['adapter_sha256']}})
        self.transport()
        with mock.patch.object(inference_identity, 'runtime_roots', return_value=[]), \
                mock.patch.object(inference_identity, 'code_identity', return_value='c' * 64), \
                mock.patch.object(inference_identity, 'device_identity', return_value={'backend': 'fixture'}):
            old = self.record_receipt()
            self.assertIsNotNone(inference_identity.admit(self.state, self.argv, adapter.EDIT_INPUT_VIEW))
            policy = json.loads((self.state / 'live-selector.json').read_text())
            with mock.patch.object(selector_heal, 'official_runtime', return_value=(sys.executable, str(self.model))), \
                    mock.patch.object(selector_heal, 'rebind', return_value=(policy, False)), \
                    mock.patch.object(selector_heal, 'pressure', return_value=None), \
                    mock.patch.object(selector_heal, 'attest'), \
                    mock.patch.object(selector_heal, 'prepare', side_effect=self.record_receipt) as prepare, \
                    mock.patch.object(default_enrollment, '_activate_profile') as activate:
                result = default_enrollment.prepare(self.project, self.install, 'fixture', lease_fd=123)
                self.assertEqual(result['status'], 'selected')
                prepare.assert_called_once()
                activate.assert_called_once()
            current = inference_identity.admit(self.state, self.argv, adapter.EDIT_INPUT_VIEW)
            self.assertNotEqual(current['checkpoint_sha256'], old['checkpoint_sha256'])
            self.assertEqual(json.loads((self.state / 'active-checkpoint.json').read_text())['schema_version'], 1)
            self.assertEqual(len(self.seen), 1)

    def test_pointer_with_other_changes_is_refused_without_rewrite(self):
        active = self.namespace()
        active.pop('schema_version', None)
        active['artifact_sha256'] = '0' * 64
        self.write(self.state / 'active-checkpoint.json', active)
        before = (self.state / 'active-checkpoint.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'default_learning_namespace_changed'):
            self.namespace()
        self.assertEqual((self.state / 'active-checkpoint.json').read_bytes(), before)

    def test_previous_cache_policy_rebinds_before_pointer_repair_and_real_warm(self):
        self._previous_cache_prepare('1.0.9', 'claude')

    def test_previous_codex_cache_policy_is_also_the_same_namespace(self):
        self._previous_cache_prepare('1.0.10', 'codex')

    def test_removed_previous_cache_anchor_can_rebind_and_repair(self):
        self._previous_cache_prepare('1.0.9', 'claude', present=False)

    def _previous_cache_prepare(self, version, host, *, present=True):
        from memvane.learning.context_learning import adapter_binding
        active = self.namespace()
        active.pop('schema_version')
        self.write(self.state / 'active-checkpoint.json', active)
        old = self.home / ('.' + host) / 'plugins/cache/memvane-marketplace/memvane' / version
        anchor = old / 'memvane/learning/context_learning/adapter.py'
        anchor.parent.mkdir(mode=0o700, parents=True)
        anchor.write_bytes(Path(self.argv[1]).read_bytes())
        anchor.chmod(0o600)
        host_manifest = old / ('.' + host + '-plugin') / 'plugin.json'
        host_manifest.parent.mkdir(mode=0o700)
        for path in (old / 'package.json', host_manifest):
            self.write(path, {'name': 'memvane', 'version': version})
        if not present:
            anchor.unlink()
        policy = json.loads((self.state / 'live-selector.json').read_text())
        policy['trainerArgv'][1] = str(anchor)
        self.write(self.state / 'live-selector.json', policy)
        self.write(self.state / 'laya-synthetic-attestation.json',
                   {'attestation': {'adapter_sha256': self.verified['adapter_sha256']}})
        self.transport()
        with mock.patch.object(inference_identity, 'runtime_roots', return_value=[]), \
                mock.patch.object(inference_identity, 'code_identity', return_value='c' * 64), \
                mock.patch.object(inference_identity, 'device_identity', return_value={'backend': 'fixture'}), \
                mock.patch.object(selector_heal, 'official_runtime', return_value=(sys.executable, str(self.model))), \
                mock.patch.object(selector_heal, '_official') as official, \
                mock.patch.object(adapter_binding, '_local_binding', side_effect=ValueError('fixture source')), \
                mock.patch.object(selector_heal, 'pressure', return_value=None), \
                mock.patch.object(selector_heal, 'attest'), \
                mock.patch.object(selector_heal, 'prepare', side_effect=self.record_receipt) as prepare, \
                mock.patch.object(default_enrollment, '_activate_profile'):
            result = default_enrollment.prepare(self.project, self.install, 'fixture', lease_fd=123)
            self.assertEqual(result['status'], 'selected')
            prepare.assert_called_once()
            official.assert_called_once()
        self.assertEqual(json.loads((self.state / 'live-selector.json').read_text())['trainerArgv'], self.argv)
        self.assertEqual(json.loads((self.state / 'active-checkpoint.json').read_text())['schema_version'], 1)
        self.assertEqual(len(self.seen), 1)

    def test_cache_path_difference_does_not_bypass_trainer_admission(self):
        active = self.namespace()
        active.pop('schema_version')
        self.write(self.state / 'active-checkpoint.json', active)
        path = self.state / 'live-selector.json'
        policy = json.loads(path.read_text())
        policy['trainerArgv'][1] = str(self.home / 'missing-old-cache/adapter.py')
        self.write(path, policy)
        with mock.patch.object(selector_heal, '_official', side_effect=ValueError('unproved current trainer')):
            with self.assertRaisesRegex(selector_heal.HealRefused, 'adapter_not_official'):
                self.namespace()
        self.assertEqual(json.loads((self.state / 'active-checkpoint.json').read_text()), active)
        self.assertEqual(json.loads(path.read_text()), policy)

    def test_old_cache_path_does_not_allow_other_policy_changes(self):
        self.namespace()
        path = self.state / 'live-selector.json'
        policy = json.loads(path.read_text())
        policy['trainerArgv'][1] = str(self.home / '.codex/plugins/cache/memvane-marketplace/memvane/1.0.10/memvane/learning/context_learning/adapter.py')
        policy['timeoutSeconds'] = 31
        self.write(path, policy)
        before = path.read_bytes()
        with mock.patch.object(selector_heal, 'rebind') as rebind:
            with self.assertRaisesRegex(ValueError, 'default_learning_namespace_changed'):
                self.namespace()
            rebind.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
