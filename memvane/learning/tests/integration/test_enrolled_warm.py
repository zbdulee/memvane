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
        stack.enter_context(mock.patch.object(inference_service._IDENTITY_CACHE, 'verify', return_value=self.verified))

    def test_enrollment_pointer_passes_real_warm_validator_and_live_selector(self):
        active = self.namespace()
        self.transport()
        self.assertEqual(selector_heal.warm(self.project, state=self.state)['status'], 'selector_warm')
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
