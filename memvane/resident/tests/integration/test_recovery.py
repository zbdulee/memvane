"""Resident retry state and admission under a controlled clock."""
from pathlib import Path

from tests.support.case import FixtureCase
from unittest.mock import patch
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as svc
from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred


class RecoveryChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home / 'r'
        self.base.mkdir(mode=0o700)
        self.manifest = {'serviceSocketPath': str(self.base / 'embed.sock'),
                         'embeddingFingerprint': 'a' * 64,
                         'approvedProviderId': 'synthetic',
                         'providerArtifactSha256': 'b' * 64,
                         'modelId': 'synthetic-model',
                         'dimension': 4}

    def stamp(self, reason, *, terminal=True, attempts=3):
        with patch.object(owner.time, 'time', return_value=1000):
            owner._write_state(self.manifest, reason, terminal=terminal, attempts=attempts)

    def cold(self, now):
        return (patch.object(owner, 'probe', return_value='absent'),
                patch.object(owner.time, 'time', return_value=now))

    def test_legacy_transient_terminal_recovers_after_cooldown(self):
        for reason in ('startup_failed', 'worker_exited'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(1301)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_called_once()

    def test_transient_terminal_waits_without_extending_cooldown(self):
        self.stamp('startup_failed')
        probe, clock = self.cold(1299)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        self.assertEqual(owner.read_owner_state(self.manifest)['at'], 1000)

    def test_security_terminal_never_expires(self):
        for reason in ('attestation_invalid', 'identity_invalid'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(100000)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_not_called()

    def test_abrupt_third_launch_enters_recoverable_cooldown(self):
        self.stamp('ready', terminal=False)
        probe, clock = self.cold(1021)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()
        probe, clock = self.cold(1322)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_called_once()

    def test_service_retries_after_three_start_failures(self):
        project = str(self.base / 'project')
        Path(project).mkdir()
        decision = {'projectRoot': project}
        proof = {'providerManifest': self.manifest}
        calls, attests = [], []
        def start_worker(*_args):
            calls.append(1)
            if len(calls) <= 3:
                raise RuntimeError('synthetic_ready_timeout')
            return object()
        (self.base / 'svc').mkdir(mode=0o700)
        service = svc.Service(self.base / 'svc', bundle={'version': '1.0.0', 'digest': 'a'},
                              prove=lambda _p: (decision, proof, 'selected'),
                              attest=lambda *_a: attests.append(1),
                              start_worker=start_worker, admit=lambda: True,
                              )
        key = svc._worker_key(self.manifest)
        worker = svc.Worker(key, str(self.base / 'worker.sock'))
        service.workers[key] = worker
        for now in (1000, 1011, 1032):
            with patch.object(svc.time, 'monotonic', return_value=now), \
                 patch.object(owner.time, 'time', return_value=now):
                service._load_attested(project, decision, proof, 'selected',
                                       self.manifest, worker, 'seal')
        class InlineThread:
            def __init__(self, *, target, args, **_kw):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        with patch.object(svc.threading, 'Thread', InlineThread), \
             patch.object(svc.time, 'monotonic', return_value=1333), \
             patch.object(owner.time, 'time', return_value=1333):
            service._load(project, decision, proof, 'selected', self.manifest, key, 'seal')
        self.assertEqual((len(calls), len(attests), worker.state), (4, 4, 'ready'))

    def test_repeated_moved_selection_deferrals_back_off_as_start_failures(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        busy = ResourceDeferred('writer_proof_physical_busy')
        delays = [service._deferred_retry('/project', self.manifest, moved)
                  for _ in range(svc.MOVED_DEFER_LIMIT + 2)]
        state = owner.read_owner_state(self.manifest)
        self.assertEqual(len(set(delays[:svc.MOVED_DEFER_LIMIT])), 1)
        self.assertGreater(delays[-1], delays[-2])
        self.assertEqual((state['reason'], state['attempts'], state['terminal']),
                         ('startup_failed', 2, False))
        self.assertEqual(service._deferred_retry('/other', self.manifest, busy), delays[0])
        self.assertNotIn('/other', service.moved)

    def test_moved_deferral_budget_isolated_by_installation_socket(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT + 2):
            service._deferred_retry('/project', self.manifest, moved)
        other = dict(self.manifest, serviceSocketPath=str(self.base / 'other.sock'))
        self.assertEqual(service._deferred_retry('/project', other, moved), 5)
        self.assertIsNone(owner.read_owner_state(other))

    def test_other_deferral_breaks_consecutive_moved_sequence(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT):
            service._deferred_retry('/project', self.manifest, moved)
        service._deferred_retry('/project', self.manifest, ResourceDeferred('writer_proof_physical_busy'))
        self.assertEqual(service._deferred_retry('/project', self.manifest, moved), 5)
        self.assertIsNone(owner.read_owner_state(self.manifest))

    def test_moved_deferral_preserves_prior_attempt_after_restart(self):
        self.stamp('startup_failed', terminal=False, attempts=2)
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        with patch.object(owner.time, 'time', return_value=1011):
            delay = service._deferred_retry('/project', self.manifest,
                                            ResourceDeferred('worker_selection_changed'))
        self.assertGreater(delay, 5)
        self.assertEqual(owner.read_owner_state(self.manifest)['attempts'], 3)

    def test_restart_preserves_previous_startup_failure_count(self):
        self.stamp('startup_failed', terminal=False, attempts=2)
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        class ParkedThread:
            def __init__(self, **_kwargs):
                pass
            def start(self):
                pass
        key = svc._worker_key(self.manifest)
        with patch.object(svc.threading, 'Thread', ParkedThread), \
             patch.object(owner.time, 'time', return_value=1011):
            service._load(project, {'projectRoot': project}, {}, 'selected',
                          self.manifest, key, 'seal')
        self.assertEqual(service.workers[key].failures, 2)
        self.assertEqual(owner.read_owner_state(self.manifest)['attempts'], 2)

    def test_resource_deferral_does_not_erase_prior_failures(self):
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        decision = {'projectRoot': project}
        proof = {'providerManifest': self.manifest}
        service = svc.Service(root, bundle={'version': '1.0.0'},
                              prove=lambda _p: (decision, proof, 'selected'),
                              admit=lambda: False)
        worker = svc.Worker(svc._worker_key(self.manifest), str(self.base / 'worker.sock'))
        worker.failures = 2
        service._load_attested(project, decision, proof, 'selected', self.manifest, worker, 'seal')
        row = owner.read_owner_state(self.manifest)
        self.assertEqual((row['reason'], row['attempts'], worker.failures),
                         ('resource_deferred', 2, 2))

    def test_warm_attach_respects_existing_failure_cooldown(self):
        self.stamp('startup_failed', terminal=False, attempts=3)
        project = str(self.base / 'project')
        Path(project).mkdir()
        root = self.base / 'svc'
        root.mkdir(mode=0o700)
        proof = {'providerManifest': self.manifest}
        attest_calls = []
        def defer(*_args):
            attest_calls.append(1)
            raise ResourceDeferred('worker_selection_changed')
        service = svc.Service(root, bundle={'version': '1.0.0'},
                              prove=lambda _p: ({'projectRoot': project}, proof, 'selected'),
                              attest=defer)
        worker = svc.Worker(svc._worker_key(self.manifest), str(self.base / 'worker.sock'))
        worker.alive = lambda: True
        service.workers[worker.identity] = worker
        with patch.object(owner, '_owner_identity', return_value={'id': 'synthetic'}), \
             patch.object(owner.time, 'time', return_value=1001):
            service._adopt(project)
        self.assertEqual(attest_calls, [])
