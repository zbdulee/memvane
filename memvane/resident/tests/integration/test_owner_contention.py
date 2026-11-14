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

    def test_incompatible_newer_service_never_spawns_or_drains(self):
        from unittest.mock import patch
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        answer = {'status': 'ready', 'protocols': [999],
                  'bundle': {'version': '9.0.0', 'digest': 'newer'}}
        with patch.object(resident_service, 'hello', return_value=answer), \
             patch.object(resident_service, 'bundle_identity',
                          return_value={'version': '1.0.0', 'digest': 'old'}), \
             patch.object(resident_service, 'exchange') as exchange, \
             patch.object(resident_service, '_spawn') as spawn:
            for _ in range(2):
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_protocol_mismatch'):
                    resident_service.ensure_service(root=root, learning_root=False)
        exchange.assert_not_called()
        spawn.assert_not_called()

    def test_equal_or_older_client_keeps_compatible_service(self):
        from unittest.mock import patch
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        answer = {'status': 'ready', 'protocols': [resident_service.PROTOCOL],
                  'bundle': {'version': '1.0.1', 'digest': 'newer'}}
        with patch.object(resident_service, 'hello', return_value=answer), \
             patch.object(resident_service, 'exchange') as exchange, \
             patch.object(resident_service, '_spawn') as spawn:
            for version in ('1.0.0', '1.0.1'):
                with patch.object(resident_service, 'bundle_identity',
                                  return_value={'version': version, 'digest': 'other'}):
                    self.assertEqual(resident_service.ensure_service(root=root), 'ready')
        exchange.assert_not_called()
        spawn.assert_not_called()

    def test_unproved_install_reports_each_reason_once_per_episode(self):
        from unittest.mock import patch
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = resident_service.Service(root, bundle={'version': '1.0.0'})
        project = '/synthetic/project'
        reasons = ('provider_owner_route_unavailable', 'invalid_evidence',
                   'provider_owner_route_unavailable', 'invalid_evidence')
        with patch.object(resident_service, '_event') as event:
            for reason in reasons:
                service._report_waiting(project, {}, reason, outcome='unproved')
            self.assertEqual([call.args[3] for call in event.call_args_list],
                             ['provider_owner_route_unavailable', 'invalid_evidence'])
            service.unproved.pop(project)
            service._report_waiting(project, {}, reasons[0], outcome='unproved')
        self.assertEqual(event.call_count, 3)

    def test_default_admission_checks_runtime_without_provider_lane(self):
        from unittest.mock import Mock, patch
        from memvane.runtime import api as runtime_api
        controller = Mock()
        controller.check.return_value = {'ready': False}
        controller.activity.side_effect = AssertionError('provider lane must remain free')
        report = {'ready': True, 'synthetic': True}
        with patch.object(runtime_api, 'coordinator', return_value=controller), \
             patch.object(runtime_api, 'resource_report', return_value=report) as observe:
            self.assertFalse(resident_service.default_admit())
        self.assertEqual(controller.check.call_count, 1)
        self.assertEqual(controller.check.call_args.args[1], report)
        self.assertEqual(observe.call_args.kwargs, {'detailed': True})
        controller.activity.assert_not_called()

    def test_default_worker_start_checks_provider_and_worker_hashes(self):
        from unittest.mock import patch
        from memvane.kernel.errors import IndexContractError
        from memvane.provider import api as provider_api
        manifest = {'providerArtifactSha256': 'a' * 64, 'workerSha256': 'b' * 64}
        paths = [self.home / 'provider', self.home / 'worker']
        with patch.object(resident_service, 'bundle_file', side_effect=paths), \
             patch.object(provider_api, 'hash_file', side_effect=['a' * 64, 'b' * 64]), \
             patch.object(provider_api, 'start_owned_embed_service', return_value='handle') as start:
            self.assertEqual(resident_service.default_start_worker(manifest, lambda: True), 'handle')
        self.assertEqual(start.call_args.args[0]['providerPath'], str(paths[0]))
        self.assertEqual(start.call_args.args[0]['workerPath'], str(paths[1]))
        with patch.object(resident_service, 'bundle_file', side_effect=paths), \
             patch.object(provider_api, 'hash_file', return_value='f' * 64), \
             patch.object(provider_api, 'start_owned_embed_service') as start:
            with self.assertRaisesRegex(IndexContractError, 'provider_attestation_invalid'):
                resident_service.default_start_worker(manifest, lambda: True)
        start.assert_not_called()

    def test_slow_sender_cannot_extend_the_absolute_read_deadline(self):
        from unittest.mock import patch
        class SlowSender:
            def __init__(self):
                self.timeouts = []
                self.reads = 0

            def settimeout(self, value):
                self.timeouts.append(value)

            def recv(self, _limit):
                self.reads += 1
                return b'x'

        sender = SlowSender()
        with patch.object(resident_service.time, 'monotonic', side_effect=[1.0, 2.0, 3.0, 4.0]):
            with self.assertRaisesRegex(TimeoutError, 'laya_service_deadline'):
                resident_service._read_all(sender, 100, 3.5)
        self.assertEqual(sender.timeouts, [2.5, 1.5, .5])
        self.assertEqual(sender.reads, 3)

    def test_two_projects_request_one_service_spawn(self):
        import fcntl
        import os
        from unittest.mock import patch
        from memvane.resident.tests.support import manifest_for
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        spawned = []
        with patch.object(resident_service, '_learning_root', return_value=None), \
             patch.object(embed_resident, '_failure_decision'):
            for index in (1, 2):
                project = self.home / ('p%d' % index)
                project.mkdir(mode=0o700)
                manifest = manifest_for(self.home / ('e%d.sock' % index))
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                    embed_resident.ensure_running(str(project), manifest, service_root=root,
                        spawn=lambda argv, **_kwargs: spawned.append(argv))
                if index == 1:
                    held = os.open(root / resident_service.SUCCESSOR, os.O_RDWR)
                    fcntl.flock(held, fcntl.LOCK_EX)
                    self.addCleanup(os.close, held)
        self.assertEqual(len(spawned), 1)
        self.assertIn('resident-service', spawned[0])
        self.assertIn(str(root), spawned[0])
        service = resident_service.Service(root, bundle={'version': '1.0.0'})
        self.assertEqual(len(service.registered()), 2)

    def test_fixture_never_spawns_implicit_live_service(self):
        from unittest.mock import patch
        from memvane.resident.tests.support import manifest_for
        manifest = manifest_for(self.home / 'e.sock')
        with patch.object(resident_service, '_spawn') as spawn, \
             patch.object(embed_resident, '_failure_decision'):
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_fixture'):
                embed_resident.ensure_running('/synthetic/project', manifest)
        spawn.assert_not_called()
