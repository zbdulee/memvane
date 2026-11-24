"""Resident retry state and admission under a controlled clock."""
import json
from pathlib import Path

from tests.support.case import FixtureCase
from unittest.mock import Mock, patch
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as svc
from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred


class InlineThread:
    def __init__(self, *, target, args, **_kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


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

    def test_identity_failure_remains_terminal(self):
        self.stamp('identity_invalid')
        probe, clock = self.cold(100000)
        with probe, clock, patch.object(svc, 'request_adopt') as adopt:
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                owner.ensure_running('/synthetic/project', self.manifest)
        adopt.assert_not_called()

    def test_attestation_retries_from_the_recorded_time_or_after_clock_rollback(self):
        self.stamp('attestation_invalid')
        for now, retry in ((4599, False), (4600, True), (999, True)):
            with self.subTest(now=now):
                probe, clock = self.cold(now)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    if retry:
                        with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                            owner.ensure_running('/project', self.manifest)
                        adopt.assert_called_once()
                    else:
                        with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                            owner.ensure_running('/project', self.manifest)
                        adopt.assert_not_called()
                self.assertEqual(owner.read_owner_state(self.manifest)['at'], 1000)

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
        delays = [service._deferred_retry('/project', self.manifest, moved, 0)
                  for _ in range(svc.MOVED_DEFER_LIMIT + 2)]
        state = owner.read_owner_state(self.manifest)
        self.assertEqual(len(set(delays[:svc.MOVED_DEFER_LIMIT])), 1)
        self.assertGreater(delays[-1], delays[-2])
        self.assertEqual((state['reason'], state['attempts'], state['terminal']),
                         ('startup_failed', 2, False))
        self.assertEqual(service._deferred_retry('/other', self.manifest, busy, 0), delays[0])
        self.assertNotIn('/other', service.moved)

    def test_moved_deferral_budget_isolated_by_installation_socket(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT + 2):
            service._deferred_retry('/project', self.manifest, moved, 0)
        other = dict(self.manifest, serviceSocketPath=str(self.base / 'other.sock'))
        self.assertEqual(service._deferred_retry('/project', other, moved, 0), 5)
        self.assertIsNone(owner.read_owner_state(other))

    def test_other_deferral_breaks_consecutive_moved_sequence(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        moved = ResourceDeferred('worker_selection_changed')
        for _ in range(svc.MOVED_DEFER_LIMIT):
            service._deferred_retry('/project', self.manifest, moved, 0)
        service._deferred_retry('/project', self.manifest, ResourceDeferred('writer_proof_physical_busy'), 0)
        self.assertEqual(service._deferred_retry('/project', self.manifest, moved, 0), 5)
        self.assertIsNone(owner.read_owner_state(self.manifest))

    def test_moved_deferral_preserves_prior_attempt_after_restart(self):
        self.stamp('startup_failed', terminal=False, attempts=2)
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        service = svc.Service(root, bundle={'version': '1.0.0'})
        with patch.object(owner.time, 'time', return_value=1011):
            prior = owner._failure_decision(self.manifest)
            delay = service._deferred_retry('/project', self.manifest,
                                            ResourceDeferred('worker_selection_changed'), prior['attempts'])
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
        # Before proof succeeds the prior attempts belong only to this install.
        self.assertEqual(service.workers[key].failures, 0)
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

    def test_attest_retries_and_logs_each_failure_on_cold_and_warm_workers(self):
        for warm in (False, True):
            with self.subTest(warm=warm):
                root = self.base / ('warm' if warm else 'cold')
                root.mkdir(mode=0o700)
                project = str(root)
                manifest = dict(self.manifest, serviceSocketPath=str(root / 'embed.sock'))
                decision = {'projectRoot': project, 'installId': root.name,
                            'journalPath': str(root / 'install.journal')}
                proof = {'providerManifest': manifest}
                # Two identical reasons must produce two diagnostic rows.
                reasons = ['selected_route_changed', 'selected_route_changed',
                           'selection_transition_pending', 'io error', 'io error', 'io error',
                           'provider_artifact_hash_mismatch', 'provider_artifact_hash_mismatch']
                attest = Mock(side_effect=[IndexContractError(code) for code in reasons] + [None])
                service = svc.Service(root, bundle={'version': '1.1.1'},
                                      prove=lambda _p: (decision, proof, 'selected'),
                                      attest=attest, admit=lambda: True, start_worker=Mock())
                key = svc._worker_key(manifest)
                if warm:
                    worker = service.workers[key] = svc.Worker(key, str(root / 'worker.sock'))
                    worker.alive = lambda: True
                with patch.object(owner, '_owner_identity', return_value='fixture'), \
                     patch.object(svc.threading, 'Thread', InlineThread), \
                     patch.object(owner.time, 'time') as clock:
                    def adopt(at):
                        clock.return_value = at
                        service._adopt(project)
                        return owner.read_owner_state(manifest)

                    for now in (100, 110, 120):
                        row = adopt(now)
                        self.assertFalse(row and row['terminal'])
                        self.assertEqual(row['attempts'] if row else 0, 0)
                    for count, now in enumerate((130, 140, 150), 1):
                        row = adopt(now)
                        self.assertEqual((row['reason'], row['attempts'], row['terminal']),
                                         ('startup_failed', count, False))
                    adopt(449)
                    self.assertEqual(attest.call_count, 6)
                    row = adopt(450)
                    self.assertEqual((row['reason'], row['terminal'], row['at']),
                                     ('attestation_invalid', True, 450))
                    adopt(4049)
                    self.assertEqual(attest.call_count, 7)
                    self.assertEqual(adopt(4050)['at'], 4050)
                    adopt(7649)
                    self.assertEqual(attest.call_count, 8)
                    service.start_worker.assert_not_called()
                    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
                    self.assertEqual([(e['event'], e['install'], e['outcome'], e['reason']) for e in events],
                                     [('resident.adopt', root.name, 'waiting' if i < 3 else 'unproved',
                                       'IndexContractError' if code == 'io error' else code)
                                      for i, code in enumerate(reasons)])
                    # A repaired installation actually starts or binds on the next due proof.
                    with patch.object(service, '_accept_loop'):
                        adopt(7650)
                    self.assertEqual(attest.call_count, 9)
                    if warm:
                        self.assertEqual(owner.read_owner_state(manifest)['reason'], 'ready')
                        service._release(project, reason='starting')
                    else:
                        service.start_worker.assert_called_once()

    def test_worker_launch_rejection_also_reproves_after_one_hour(self):
        manifest = self.manifest
        project = str(self.base)
        decision = {'projectRoot': project}
        proof = {'providerManifest': manifest}
        start = Mock(side_effect=[IndexContractError('provider_recipe_mismatch'), object()])
        service = svc.Service(self.base, bundle={'version': '1.1.1'}, attest=Mock(),
                              start_worker=start, admit=lambda: True)
        key = svc._worker_key(manifest)
        with patch.object(svc.threading, 'Thread', InlineThread), patch.object(owner.time, 'time') as clock:
            for at, starts in ((100, 1), (3699, 1), (3700, 2)):
                clock.return_value = at
                service._load(project, decision, proof, 'selected', manifest, key, 'seal')
                self.assertEqual((service.attest.call_count, start.call_count), (starts, starts))
                if starts == 1:
                    row = owner.read_owner_state(manifest)
                    self.assertEqual((row['reason'], row['terminal'], row['at']),
                                     ('attestation_invalid', True, 100))
                    self.assertNotIn('seal', service.attested)
        self.assertEqual(service.workers[key].state, 'ready')
