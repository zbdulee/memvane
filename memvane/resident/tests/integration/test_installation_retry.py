"""Project-local launch proofs and resident adoption deadlines."""
from contextlib import contextmanager
import errno
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support.case import FixtureCase
from memvane.kernel import bundle_paths
from memvane.provider import api as provider
from memvane.resident import embed_resident as owner, resident_service as svc


class InstallationRetry(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.home / 'r'
        self.root.mkdir(mode=0o700)
        self.projects = []
        self.proofs = {}
        worker = bundle_paths.bundle_file(bundle_paths.WORKER_RELATIVE_PATH)
        artifact = bundle_paths.bundle_file(bundle_paths.PROVIDER_RELATIVE_PATH)
        for name in ('a', 'b'):
            path = self.root / name
            path.mkdir(mode=0o700)
            project = str(path)
            self.projects.append(project)
            manifest = {'approvedProviderId': 'synthetic', 'embeddingFingerprint': 'a' * 64,
                        'workerPath': str(worker), 'workerSha256': provider.hash_file(str(worker)),
                        'providerPath': str(artifact), 'providerArtifactSha256': provider.hash_file(str(artifact)),
                        'dimension': 4, 'modelId': 'fixture', 'modelSha256': 'c' * 64,
                        'serviceSocketPath': str(path / 'e.sock')}
            decision = dict(projectRoot=project, journalPath=str(path / 'install.sqlite'),
                            installId=name, scopeId=name, storePath=str(path / 'store'),
                            storeIncarnation=name, vectorProfile={})
            self.proofs[project] = (decision, {'providerManifest': manifest}, 'selected')
        self.service = svc.Service(self.root, bundle={'version': '1.1.1'},
                                   prove=Mock(side_effect=self.proofs.__getitem__),
                                   attest=Mock(), admit=lambda: True)

    def test_launch_rejection_or_read_error_only_charges_its_install(self):
        a, b = self.projects
        decision, proof, mode = self.proofs[a]
        ma = proof['providerManifest']
        mb = self.proofs[b][1]['providerManifest']
        for project in self.projects:
            svc.register(project, root=self.root)
        key = svc._worker_key(ma)
        self.assertEqual(key, svc._worker_key(mb))
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                worker = self.service.workers[key] = svc.Worker(key, str(self.root / 'w.sock'))
                worker.loading = True
                ma['workerSha256'] = '1' * 64 if rejected else mb['workerSha256']
                owner._write_state(mb, 'ready')
                before = Path(mb['serviceSocketPath'] + '.resident.state').read_bytes()
                def read(path):
                    if not rejected:
                        raise OSError(errno.EIO, 'fixture read')
                    return real_hash(path)
                real_hash = provider.hash_file
                with patch.object(provider, 'hash_file', side_effect=read), \
                     patch.object(provider, 'start_owned_embed_service') as launch:
                    self.service._load_attested(a, decision, proof, mode, ma, worker, 'seal-a')
                    launch.assert_not_called()
                state = owner.read_owner_state(ma)
                self.assertEqual((state['reason'], state['attempts'], state['terminal']),
                                 ('attestation_invalid', 3, True) if rejected else ('startup_failed', 1, False))
                self.assertEqual(Path(mb['serviceSocketPath'] + '.resident.state').read_bytes(), before)
                self.assertEqual((worker.failures, worker.retry_at, worker.loading), (0, 0, False))
                with patch.object(provider, 'start_owned_embed_service', return_value=object()) as launch:
                    bd, bp, bm = self.proofs[b]
                    self.service._load_attested(b, bd, bp, bm, mb, worker, 'seal-b')
                    launch.assert_called_once()
                self.assertEqual(worker.state, 'ready')

    def restart(self):
        self.service = svc.Service(self.root, bundle=self.service.bundle, prove=self.service.prove,
                                   attest=self.service.attest, admit=self.service.admit)

    @contextmanager
    def inline_clock(self):
        for project in self.projects:
            svc.register(project, root=self.root)
        def inline(**opts):
            return Mock(start=lambda: opts['target'](*opts['args']))
        with patch.object(svc.threading, 'Thread', side_effect=inline), \
             patch.object(owner.time, 'time') as clock, patch.object(svc.time, 'monotonic', clock):
            yield clock

    def test_actual_launch_failures_survive_service_replacement(self):
        a, b = self.projects
        ma, mb = [self.proofs[p][1]['providerManifest'] for p in (a, b)]
        key = svc._worker_key(ma)
        with self.inline_clock() as clock, patch.object(provider, 'start_owned_embed_service',
                                                       side_effect=RuntimeError('one_spawn_failure')) as launch:
            for moment, count in ((100, 1), (110, 2)):
                clock.return_value = moment
                self.service._adopt(a)
                self.assertEqual((launch.call_count, owner.read_owner_state(mb)['attempts']), (count, count))
            self.restart()
            clock.return_value = 129
            self.service._adopt(b)
            self.assertEqual(launch.call_count, 2)
            clock.return_value = 130
            self.service._adopt(a)
            worker = self.service.workers[key]
            self.assertEqual((launch.call_count, worker.failures, worker.state, worker.retry_at,
                              owner.read_owner_state(mb)['attempts']), (3, 3, 'cooldown', 430, 3))
            self.restart()
            clock.return_value = 429
            self.service._adopt(b)
            self.assertEqual(launch.call_count, 3)
            clock.return_value = 430
            launch.side_effect = None
            self.service._adopt(b)
            self.assertEqual((launch.call_count, self.service.workers[key].failures), (4, 0))
            self.restart()
            clock.return_value = 440
            launch.side_effect = RuntimeError('new_failure')
            self.service._adopt(a)
            self.assertEqual((launch.call_count, self.service.workers[key].failures), (5, 1))

    def test_resource_wait_before_and_after_install_cooldown(self):
        a, b = self.projects
        mb = self.proofs[b][1]['providerManifest']
        with self.inline_clock() as clock:
            with patch.object(provider, 'hash_file', side_effect=OSError(errno.EIO, 'proof read')):
                for count, moment in enumerate((70, 80, 100), 1):
                    clock.return_value = moment
                    self.service._adopt(b)
                    self.assertEqual(owner.read_owner_state(mb)['attempts'], count)
            exhausted = owner.read_owner_state(mb)
            self.assertEqual(self.service.retry[b], 400)
            with patch.object(self.service, 'admit', return_value=False):
                clock.return_value = 395
                self.service._adopt(a)
                self.assertEqual(owner.read_owner_state(mb), exhausted)
                clock.return_value = 401
                self.service._adopt(a)
                deferred = owner.read_owner_state(mb)
                self.assertEqual((deferred['reason'], deferred['attempts']), ('resource_deferred', 0))
            clock.return_value = 407
            with patch.object(provider, 'start_owned_embed_service', return_value=object()) as launch:
                self.service._adopt(b)
                launch.assert_called_once()
                self.assertFalse(owner.read_owner_state(mb)['terminal'])

    def check_shared_launch_retry(self, *, prior_reads, deferred=False, restart=False):
        a, b = self.projects
        ma, mb = [self.proofs[p][1]['providerManifest'] for p in (a, b)]
        key = svc._worker_key(ma)
        self.assertEqual(key, svc._worker_key(mb))
        for p in (a, b):
            svc.register(p, root=self.root)

        def inline(**opts):
            return Mock(start=lambda: opts['target'](*opts['args']))

        with patch.object(svc.threading, 'Thread', side_effect=inline), \
             patch.object(owner.time, 'time') as wall, patch.object(svc.time, 'monotonic') as mono:
            for attempts, stamp in enumerate(prior_reads, 1):
                wall.return_value = mono.return_value = stamp
                with patch.object(provider, 'hash_file', side_effect=OSError(errno.EIO, 'fixture launch proof read')):
                    self.service._adopt(a)
                self.assertEqual(owner.read_owner_state(ma)['attempts'], attempts)
                self.assertIsNone(owner.read_owner_state(mb))
                self.assertEqual(self.service.workers[key].failures, 0)

            if restart:
                self.restart()
            if deferred:
                wall.return_value = mono.return_value = 130
                with patch.object(self.service, 'admit', return_value=False):
                    self.service._adopt(a)
                self.assertEqual(self.service.workers[key].failures, 0)
                self.assertEqual(owner.read_owner_state(ma)['attempts'], len(prior_reads))
                self.assertEqual(owner.read_owner_state(mb)['attempts'], 0)

            wall.return_value = mono.return_value = 160 if deferred else 130
            with patch.object(provider, 'start_owned_embed_service', side_effect=RuntimeError('one_spawn_failure')) as launch:
                self.service._adopt(a)
                launch.assert_called_once()
            worker = self.service.workers[key]
            self.assertEqual((worker.failures, worker.state, worker.retry_at), (1, 'failed', mono.return_value + 10))
            row = owner.read_owner_state(mb)
            self.assertEqual((row['reason'], row['attempts'], row['terminal']), ('startup_failed', 1, False))

            wall.return_value = mono.return_value = mono.return_value + 21
            with patch.object(provider, 'start_owned_embed_service', return_value=object()) as launch:
                self.service._adopt(b)
                launch.assert_called_once()
            self.assertEqual((worker.state, worker.failures), ('ready', 0))

    def test_shared_launch_failure_after_two_install_proof_errors(self):
        self.check_shared_launch_retry(prior_reads=(100, 110))

    def test_restart_does_not_restore_install_proof_errors_as_process_failures(self):
        self.check_shared_launch_retry(prior_reads=(100, 110), restart=True)

    def test_admission_propagation_normalizes_only_expired_budgets(self):
        b = self.proofs[self.projects[1]][1]['providerManifest']
        for reason, terminal, period in (('attestation_invalid', True, 3600),
                                         ('startup_failed', False, 300), ('worker_exited', True, 300)):
            for age in (period - 1, period, period + 1, -1):
                with self.subTest(reason=reason, age=age):
                    with patch.object(owner.time, 'time', return_value=100):
                        owner._write_state(b, reason, terminal=terminal, attempts=3)
                    prior = owner.read_owner_state(b)
                    with patch.object(owner.time, 'time', return_value=100 + age):
                        owner._record_shared_wait(b, 'resource_deferred', False, 0)
                    row = owner.read_owner_state(b)
                    if age == period - 1:
                        self.assertEqual(row, prior)
                    else:
                        self.assertEqual((row['reason'], row['attempts']), ('resource_deferred', 0))

    def test_old_saturated_admission_does_not_become_a_process_exit(self):
        b = self.proofs[self.projects[1]][1]['providerManifest']
        with patch.object(owner.time, 'time', return_value=100):
            owner._write_state(b, 'resource_deferred', attempts=3)
        with patch.object(owner.time, 'time', return_value=105):
            self.assertIsNone(owner._failure_decision(b))
            self.assertFalse(owner.read_owner_state(b)['terminal'])

    def test_shared_launch_failure_without_prior_proof_errors(self):
        self.check_shared_launch_retry(prior_reads=())

    def test_admission_wait_keeps_proof_history_local(self):
        self.check_shared_launch_retry(prior_reads=(100, 110), deferred=True)

    def test_shared_start_failure_preserves_another_installs_proof_cooldown(self):
        a, b = self.projects
        for project in (a, b):
            svc.register(project, root=self.root)
        ma, mb = [self.proofs[p][1]['providerManifest'] for p in (a, b)]
        with patch.object(owner.time, 'time', return_value=100):
            owner._write_state(ma, 'startup_failed', attempts=3)
        prior = owner.read_owner_state(ma)
        worker = svc.Worker(svc._worker_key(mb), str(self.root / 'w.sock'))
        with patch.object(owner.time, 'time', return_value=120), \
             patch.object(provider, 'start_owned_embed_service', side_effect=RuntimeError('one_spawn_failure')):
            self.service._load_attested(b, *self.proofs[b], mb, worker, 'seal-b')
        self.assertEqual(owner.read_owner_state(ma), prior)
        self.assertEqual((worker.failures, owner.read_owner_state(mb)['attempts']), (1, 1))

    def test_transient_state_in_the_future_allows_adoption(self):
        manifest = self.proofs[self.projects[0]][1]['providerManifest']
        for reason, attempts in (('startup_failed', 1), ('startup_failed', 2), ('starting', 0), ('resource_deferred', 0)):
            with self.subTest(reason=reason, attempts=attempts):
                with patch.object(owner.time, 'time', return_value=4000):
                    owner._write_state(manifest, reason, attempts=attempts)
                with patch.object(owner.time, 'time', return_value=400):
                    self.assertEqual(owner._failure_decision(manifest)['reason'], reason)

    def test_exhausted_transient_adoption_uses_remaining_cooldown(self):
        project = self.projects[0]
        manifest = self.proofs[project][1]['providerManifest']
        with patch.object(owner.time, 'time', return_value=100):
            owner._write_state(manifest, 'startup_failed', attempts=3)
        with patch.object(owner.time, 'time', return_value=385), patch.object(svc.time, 'monotonic', return_value=800):
            self.service._adopt(project)
        self.assertEqual(self.service.retry[project], 815)
        self.service.attest.assert_not_called()

    def test_cold_and_warm_adoption_wait_until_the_persisted_deadline(self):
        project = self.projects[0]
        decision, proof, _ = self.proofs[project]
        manifest = proof['providerManifest']
        key = svc._worker_key(manifest)
        with patch.object(self.service, 'registered', return_value=[project]), \
             patch.object(owner.time, 'time') as wall, patch.object(svc.time, 'monotonic') as mono:
            for warm in (False, True):
                with self.subTest(warm=warm):
                    wall.return_value = mono.return_value = 100
                    self.service.retry.clear()
                    self.service.prove.reset_mock()
                    self.service._attest_failed(project, decision, manifest, ValueError('provider_recipe_mismatch'), 0)
                    self.assertEqual(self.service.retry[project], 400)
                    # Restart near the end of the hour loses the in-memory timer.
                    self.service.retry.clear()
                    worker = self.service.workers[key] = svc.Worker(key, str(self.root / 'w.sock'))
                    worker.alive = lambda: warm
                    for at in (3610, 3615, 3620, 3699):
                        wall.return_value = mono.return_value = at
                        self.service.tick()
                    self.assertEqual(self.service.retry[project], 3700)
                    self.assertEqual(self.service.prove.call_count, 1)
                    self.service.attest.assert_not_called()
                    wall.return_value = mono.return_value = 3700
                    with patch.object(self.service, 'attest', side_effect=ValueError('provider_recipe_mismatch')) as attest, \
                         patch.object(svc.threading, 'Thread', side_effect=lambda **kw: Mock(start=lambda: kw['target'](*kw['args']))):
                        self.service.tick()
                        attest.assert_called_once()
                    self.assertEqual(self.service.prove.call_count, 2)
                    self.assertEqual(owner.read_owner_state(manifest)['at'], 3700)
