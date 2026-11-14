"""A busy sync owner does not invalidate a live resident binding."""
from types import SimpleNamespace
from unittest.mock import Mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.resident import embed_resident, resident_service
from tests.support.case import FixtureCase


class OwnerContention(FixtureCase):
    def test_owner_busy_keeps_live_binding(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        def busy(_project):
            raise ResourceDeferred('owner_busy')
        service = resident_service.Service(root, bundle={'version': '1.0.0'}, prove=busy)
        manifest = {key: 'synthetic' for key in embed_resident._IDENTITY}
        manifest.update(modelId='model', dimension=4)
        binding = SimpleNamespace(manifest=manifest, recheck_at=0, closed=False)
        project = '/synthetic/project'
        service.bindings[project] = binding
        service.workers[resident_service._worker_key(manifest)] = SimpleNamespace(alive=lambda: True)
        service._release = Mock()
        service._check_owned(project)
        self.assertIs(service.bindings[project], binding)
        self.assertFalse(binding.closed)
        service._release.assert_not_called()

    def test_admission_precedes_attestation_and_worker_start(self):
        from memvane.resident.tests.support import manifest_for
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        manifest = manifest_for(self.home / 'embed.sock')
        decision = {'projectRoot': str(self.home / 'p')}
        proof = {'providerManifest': manifest}
        attests, starts, admitted = [], [], [False]
        service = resident_service.Service(
            root, bundle={'version': '1.0.0'},
            attest=lambda *_args: attests.append(1),
            start_worker=lambda *_args: starts.append(1) or object(),
            admit=lambda: admitted[0])
        worker = resident_service.Worker('key', str(root / 'w.sock'))
        for _ in range(3):
            worker.loading, worker.retry_at = True, 0.0
            service._load_attested(decision['projectRoot'], decision, proof,
                                   'selected', manifest, worker, 'seal')
            self.assertEqual(worker.state, 'resource_deferred')
        self.assertEqual((attests, starts), ([], []))
        admitted[0] = True
        worker.loading, worker.retry_at = True, 0.0
        service._load_attested(decision['projectRoot'], decision, proof,
                               'selected', manifest, worker, 'seal')
        self.assertEqual((len(attests), len(starts), worker.state), (1, 1, 'ready'))

    def test_service_version_order(self):
        newer = resident_service.newer
        self.assertTrue(newer({'version': '1.0.2'}, {'version': '1.0.1'}))
        self.assertTrue(newer({'version': '1.0.1'}, {'version': '1.0.1-dev.4'}))
        self.assertTrue(newer({'version': '1.10.0'}, {'version': '1.9.9'}))
        self.assertFalse(newer({'version': '1.0.1'}, {'version': '1.0.1'}))
        self.assertFalse(newer({'version': '1.0.0'}, {'version': '1.0.1'}))

    def test_selected_and_preview_attest_before_worker_binding(self):
        from unittest.mock import patch
        from memvane.journal import api as journal_api
        from memvane.kernel.errors import IndexContractError
        manifest = {'approvedProviderId': 'synthetic'}
        proof = {'providerManifest': manifest}
        decision = {'journalPath': str(self.home / 'journal')}
        with patch.object(journal_api, 'worker_attestation', return_value=manifest) as check:
            for mode in ('selected', 'preview'):
                resident_service.default_attest(decision, proof, mode)
            self.assertEqual([call.args for call in check.call_args_list],
                             [(decision, proof, 'selected'), (decision, proof, 'preview')])
        with patch.object(journal_api, 'worker_attestation', return_value={'different': True}):
            with self.assertRaisesRegex(IndexContractError, 'provider_attestation_invalid'):
                resident_service.default_attest(decision, proof, 'selected')

    def test_invalid_socket_or_lock_is_terminal_without_start_retry(self):
        from memvane.kernel.errors import IndexContractError
        from memvane.resident.tests.support import manifest_for
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        for index, reason in enumerate(('provider_lock_invalid', 'provider_socket_invalid',
                                        'provider_socket_changed')):
            with self.subTest(reason=reason):
                manifest = manifest_for(self.home / ('e%d.sock' % index))
                proof = {'providerManifest': manifest}
                decision = {'projectRoot': str(self.home / ('p%d' % index))}
                def fail_start(*_args):
                    raise IndexContractError(reason)
                service = resident_service.Service(root, bundle={'version': '1.0.0'},
                                                   prove=lambda _project: (decision, proof, 'selected'),
                                                   attest=lambda *_args: None,
                                                   start_worker=fail_start, admit=lambda: True)
                worker = resident_service.Worker(resident_service._worker_key(manifest),
                                                 str(root / 'w.sock'))
                service._load_attested(decision['projectRoot'], decision, proof,
                                       'selected', manifest, worker, 'seal')
                state = embed_resident.read_owner_state(manifest)
                self.assertEqual((worker.state, state['reason'], state['terminal']),
                                 ('terminal', 'identity_invalid', True))
