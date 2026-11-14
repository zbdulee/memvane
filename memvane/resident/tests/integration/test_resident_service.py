"""Live resident binding and worker contracts without a model."""
import os
import threading
import time
from unittest.mock import patch

from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.resident import embed_resident as resident
from memvane.resident import resident_service as svc
from memvane.resident.tests.support import DIM, Fixture, embed


class ServiceChecks(Fixture):
    def test_public_sockets_never_appear_with_a_loose_mode(self):
        # Clients refuse a socket with group/other bits; one seen between
        # bind() and chmod() would fail a request instead of waiting.
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        public = (str(self.root / svc.CONTROL), manifest['serviceSocketPath'])
        loose, real_chmod = [], os.chmod

        def watched(path, mode, *args, **kwargs):
            for name in public:
                try:
                    info = os.lstat(name)
                except FileNotFoundError:
                    continue
                if info.st_mode & 0o077:
                    loose.append(name)
            return real_chmod(path, mode, *args, **kwargs)
        with patch.object(os, 'chmod', watched):
            self.service()
            self.wait(lambda: self.ready(manifest), message='project never adopted')
        self.assertEqual(loose, [])
        for name in public:
            self.assertEqual(os.lstat(name).st_mode & 0o777, 0o600)
        self.assertEqual(len(embed(manifest)), DIM)


    def test_one_service_serves_many_projects_concurrently_through_one_worker(self):
        projects = [self.project(name) for name in ('alpha', 'beta', 'gamma')]
        for project, _manifest in projects:
            svc.register(project, root=self.root)
        self.service()
        for _project, manifest in projects:
            self.wait(lambda m=manifest: self.ready(m), message='project never adopted')
        # Each installation keeps its own owner lease and state record.
        for _project, manifest in projects:
            self.assertTrue(resident.owner_lock_held(manifest))
            self.assertEqual(resident.read_owner_state(manifest)['reason'], 'ready')
        results, errors = [], []

        def call(manifest, kind):
            try:
                results.append(embed(manifest, kind=kind))
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(str(exc))
        threads = [threading.Thread(target=call, args=(projects[n % 3][1],
                                                        'query' if n % 2 else 'document'))
                   for n in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 24)
        self.assertEqual(len(self.loads), 1, 'one model load for every project')
        # The service never queues a seventh request at the worker, so a
        # burst from many projects never becomes an ok:false envelope.
        self.assertLessEqual(int((self.base / 'worker-max-pending').read_text()), 6)


    def test_busy_sync_owner_keeps_the_binding_and_its_socket(self):
        # The recheck proof takes the per-user sync owner lock without waiting.
        # A sync pass holding it is contention, not lost ownership: releasing
        # unlinked the socket that pass was about to probe, every time.
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service()
        self.wait(lambda: self.ready(manifest))
        binding = service.bindings[project]
        inode = os.lstat(manifest['serviceSocketPath']).st_ino
        calls = []

        def busy(_project):
            calls.append(_project)
            raise ResourceDeferred('owner_busy')
        with patch.object(resident, '_owned_route', side_effect=busy):
            service.prove = svc.default_prove
            for _ in range(20):
                service.tick()
            self.assertGreaterEqual(len(calls), 20)
            self.assertIs(service.bindings.get(project), binding)
            self.assertFalse(binding.closed)
            self.assertEqual(resident.read_owner_state(manifest)['reason'], 'ready')
            self.assertEqual(os.lstat(manifest['serviceSocketPath']).st_ino, inode)
            self.assertTrue(self.ready(manifest))
        # Owner released: the same binding still answers the sync probe.
        service.prove = self.prove
        self.assertEqual(resident.ensure_running(project, manifest).__class__,
                         resident.AttachedService)
        # A real identity change still releases the binding.
        decision, proof = self.routes[project]
        self.routes[project] = ({**decision, 'storeIncarnation': 'other'}, proof)
        self.wait(lambda: binding.closed, message='changed identity kept its binding')
        self.assertIsNot(service.bindings.get(project), binding)


    def test_ownership_proof_is_throttled_per_binding(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service()
        self.wait(lambda: self.ready(manifest))
        binding = service.bindings[project]
        calls = []
        real = self.prove

        def counted(project_):
            calls.append(project_)
            return real(project_)
        service.owner_recheck_seconds = 3600
        binding.recheck_at = time.monotonic() + 3600
        service.prove = counted
        for _ in range(20):
            service.tick()
        self.assertEqual(calls, [])
        binding.recheck_at = 0.0
        for _ in range(20):
            service.tick()
        self.assertEqual(calls, [project], 'one proof per interval')
        self.assertIs(service.bindings.get(project), binding)
        # A dead worker is noticed on the next tick, not after the interval.
        next(iter(service.workers.values())).handle.process.kill()
        self.wait(lambda: binding.closed, message='dead worker waited for the interval')


    def test_memory_gate_defers_without_terminal_failure_then_loads(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        self.admit_value = False
        with patch.object(svc, 'LOAD_RETRY_SECONDS', (0.2,)):
            self.service()
            self.wait(lambda: (resident.read_owner_state(manifest) or {}).get('reason')
                      == 'resource_deferred', message='refusal not recorded')
            row = resident.read_owner_state(manifest)
            self.assertEqual((row['terminal'], row['attempts']), (False, 0))
            self.assertEqual(self.loads, [])
            self.assertEqual(resident.probe(manifest), 'absent')
            # A query during the refusal is lexical, never a failure.
            with self.assertRaisesRegex(IndexContractError, '^provider_unavailable$'):
                embed(manifest)
            self.admit_value = True
            self.wait(lambda: self.ready(manifest), message='never loaded after admission')
        self.assertEqual(len(self.loads), 1)

    def test_worker_loaded_after_its_last_route_left_is_reclaimed(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        real_start = self.start_worker
        handles = []

        def start_worker(manifest, admit, **kw):
            handle = real_start(manifest, admit, **kw)
            self.routes.pop(project, None)  # the route left while it loaded
            handles.append(handle)
            return handle
        self.start_worker = start_worker
        service = self.service()
        service.reclaim_seconds = 0
        self.wait(lambda: handles, message='worker never loaded')
        self.wait(lambda: handles[0].process.poll() is not None,
                  message='unbound worker kept running')
        self.assertEqual(service.workers, {})
        self.assertEqual(service.bindings, {})

    def test_failed_attestation_is_terminal_for_that_installation_only(self):
        good, good_manifest = self.project('alpha')
        bad, bad_manifest = self.project('beta')
        for project in (good, bad):
            svc.register(project, root=self.root)
        service = self.service(start=False)

        def attest(decision, proof, mode):
            if decision['projectRoot'] == bad:
                raise IndexContractError('provider_attestation_invalid')
        service.attest = attest
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.wait(lambda: self.ready(good_manifest))
        self.wait(lambda: (resident.read_owner_state(bad_manifest) or {}).get('terminal') is True)
        self.assertEqual(resident.probe(bad_manifest), 'absent')
        self.assertFalse(resident.owner_lock_held(bad_manifest))

    def test_every_new_worker_start_attests_first(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        events = []
        service = self.service(start=False)
        service.attest = lambda decision, proof, mode: events.append('attest')
        start = self.start_worker

        def start_worker(manifest, admit):
            events.append('start')
            return start(manifest, admit)
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.wait(lambda: self.ready(manifest))
        self.assertEqual(events, ['attest', 'start'], 'bind must not rehash again')
        # A restart after the model process died is a new start: prove again.
        worker = next(iter(service.workers.values()))
        with patch.object(resident, '_failure_decision', return_value=None):
            worker.handle.process.kill()
            self.wait(lambda: worker.handle is None, message='worker death not observed')
            worker.retry_at = 0
            service.retry.pop(project, None)
            service.wake.set()
            self.wait(lambda: events.count('start') == 2, message='worker never restarted')
        self.assertEqual(events, ['attest', 'start', 'attest', 'start'])

    def test_failed_start_attestation_never_loads_the_model(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service(start=False)

        def attest(*_args):
            raise IndexContractError('provider_attestation_invalid')
        service.attest = attest
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.wait(lambda: (resident.read_owner_state(manifest) or {}).get('terminal') is True)
        self.assertEqual(self.loads, [])

    def test_profiles_differing_only_in_dimension_never_share_a_worker(self):
        alpha, alpha_manifest = self.project('alpha')
        beta, beta_manifest = self.project('beta', dimension=8)
        self.assertEqual(alpha_manifest['embeddingFingerprint'],
                         beta_manifest['embeddingFingerprint'])
        for project in (alpha, beta):
            svc.register(project, root=self.root)
        self.service()
        for manifest in (alpha_manifest, beta_manifest):
            self.wait(lambda m=manifest: self.ready(m))
        self.assertEqual(len(embed(alpha_manifest)), DIM)
        self.assertEqual(len(embed(beta_manifest)), 8)
        self.assertEqual(len(self.loads), 2)

    def test_drain_during_attestation_never_starts_a_worker(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        attesting, release = threading.Event(), threading.Event()

        def attest(*_args):
            attesting.set()
            release.wait(10)
        service = self.service(start=False)
        service.attest = attest
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.assertTrue(attesting.wait(5))
        service.begin_drain()
        release.set()
        self.assertTrue(service.stopped.wait(30))
        self.assertEqual(self.loads, [], 'a model started after the drain began')

    def test_failed_listener_thread_start_releases_the_binding(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        starts = []
        real = threading.Thread

        class Flaky(real):
            def start(self):
                if self.name == 'laya-service-project':
                    starts.append(1)
                    if len(starts) == 1:
                        raise RuntimeError("can't start new thread")
                return super().start()
        with patch.object(svc.threading, 'Thread', Flaky):
            self.service()
            self.wait(lambda: len(starts) >= 2, message='binding never re-adopted')
            self.wait(lambda: self.ready(manifest))
        self.assertEqual(len(embed(manifest)), DIM)

    def test_failed_listener_thread_releases_the_binding_for_readoption(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        calls = []
        real = svc.selectors.DefaultSelector

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError('selector unavailable')
            return real()
        with patch.object(svc.selectors, 'DefaultSelector', flaky):
            self.service()
            self.wait(lambda: len(calls) >= 2, message='failed binding never re-adopted')
            self.wait(lambda: self.ready(manifest))
            self.assertEqual(len(embed(manifest)), DIM)
        self.assertEqual(len(self.loads), 1)

    def test_last_release_reclaims_the_unused_worker(self):
        alpha, alpha_manifest = self.project('alpha')
        beta, beta_manifest = self.project('beta')
        for project in (alpha, beta):
            svc.register(project, root=self.root)
        service = self.service()
        service.reclaim_seconds = 0
        for manifest in (alpha_manifest, beta_manifest):
            self.wait(lambda m=manifest: self.ready(m))
        handle = next(iter(service.workers.values())).handle
        self.routes.pop(alpha)
        self.assertTrue(resident.wait_for_release(alpha_manifest['serviceSocketPath'], timeout=5))
        service._reclaim()
        self.assertIsNone(handle.process.poll(), 'a worker still bound was stopped')
        self.routes.pop(beta)
        self.wait(lambda: handle.process.poll() is not None, message='unused worker kept')
        self.wait(lambda: service.workers == {}, message='worker entry kept')

    def test_request_between_lookup_and_forward_is_never_reclaimed(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service()
        service.reclaim_seconds = 0
        self.wait(lambda: self.ready(manifest))
        handle = next(iter(service.workers.values())).handle
        entered, release = threading.Event(), threading.Event()
        real = svc.Worker.forward

        def gated(worker, *args, **kwargs):
            entered.set()
            release.wait(10)
            return real(worker, *args, **kwargs)
        results = []
        with patch.object(svc.Worker, 'forward', gated):
            thread = threading.Thread(target=lambda: results.append(
                embed(manifest, kind='document')))
            thread.start()
            self.assertTrue(entered.wait(5))
            self.routes.pop(project)  # the last binding goes away mid-request
            self.assertTrue(resident.wait_for_release(manifest['serviceSocketPath'], timeout=5))
            service._reclaim()
            self.assertIsNone(handle.process.poll(), 'worker reclaimed under a request')
            release.set()
            thread.join(30)
        self.assertEqual(len(results), 1)
        self.wait(lambda: handle.process.poll() is not None, message='never reclaimed after')

    def test_cold_loads_of_different_profiles_never_overlap(self):
        alpha, alpha_manifest = self.project('alpha')
        beta, beta_manifest = self.project('beta', model='9')
        for project in (alpha, beta):
            svc.register(project, root=self.root)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls, concurrent = [], []
        lock = threading.Lock()
        start = self.start_worker

        def start_worker(manifest, admit):
            with lock:
                calls.append(manifest['embeddingFingerprint'])
                concurrent.append(len(calls) - len(self.loads))
                started.set()
            if len(calls) == 1:
                release.wait(10)
            return start(manifest, admit)

        service = self.service(start=False)
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.assertTrue(started.wait(5))
        for _ in range(5):
            service.tick()
        self.assertEqual(len(calls), 1)
        release.set()
        for manifest in (alpha_manifest, beta_manifest):
            self.wait(lambda m=manifest: self.ready(m))
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(max(concurrent), 1)

    def test_idle_listeners_hold_no_connection_slot(self):
        manifests = []
        for name in ('alpha', 'beta', 'gamma', 'delta'):
            project, manifest = self.project(name)
            svc.register(project, root=self.root)
            manifests.append(manifest)
        with patch.object(svc, 'MAX_CONNECTIONS', 4):
            service = self.service()
            for manifest in manifests:
                self.wait(lambda m=manifest: self.ready(m))
            self.wait(lambda: service.connections._value == 4)
            for _ in range(10):
                service.tick()
                self.assertEqual(service.connections._value, 4)

    def test_drain_keeps_lease_until_loading_worker_stops(self):
        project, _manifest = self.project('alpha')
        svc.register(project, root=self.root)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        handles = []
        start = self.start_worker

        def start_worker(manifest, admit):
            entered.set()
            release.wait(10)
            handle = start(manifest, admit)
            handles.append(handle)
            return handle

        service = self.service(start=False)
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.assertTrue(entered.wait(5))
        service.begin_drain()
        self.assertTrue(service.draining.is_set())
        self.assertFalse(service.stopped.is_set())
        self.assertIsNotNone(service.lease)
        release.set()
        self.assertTrue(service.stopped.wait(30))
        self.assertEqual(len(handles), 1)
        self.assertIsNotNone(handles[0].process.poll())

    def test_worker_death_mid_request_is_provider_unavailable(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service()
        self.wait(lambda: self.ready(manifest))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = svc.Worker.forward
        errors = []

        def gated(worker, *args, **kwargs):
            entered.set()
            release.wait(10)
            return original(worker, *args, **kwargs)

        def query():
            try:
                embed(manifest)
            except Exception as exc:
                errors.append(str(exc))

        with patch.object(svc.Worker, 'forward', gated):
            thread = threading.Thread(target=query)
            thread.start()
            self.assertTrue(entered.wait(5))
            handle = next(iter(service.workers.values())).handle
            handle.process.kill()
            handle.process.wait(timeout=5)
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['provider_unavailable'])

    def test_full_attestation_runs_once_per_installation_and_only_to_bind(self):
        projects = [self.project(name) for name in ('alpha', 'beta')]
        for project, _manifest in projects:
            svc.register(project, root=self.root)
        attested = []
        service = self.service(start=False)
        service.attest = lambda decision, _proof, _mode: attested.append(decision['projectRoot'])
        self.assertTrue(service.acquire(wait=5))
        service.start()
        for _project, manifest in projects:
            self.wait(lambda item=manifest: self.ready(item))
        for _ in range(10):
            service.tick()
        self.assertEqual(sorted(attested), sorted(project for project, _manifest in projects))
        self.assertEqual(len(self.loads), 1)

    def test_busy_window_during_attestation_defers_without_a_terminal_state(self):
        projects = [self.project(name) for name in ('alpha', 'beta')]
        attempts = {project: 0 for project, _manifest in projects}
        service = self.service(start=False)

        def attest(decision, _proof, _mode):
            project = decision['projectRoot']
            attempts[project] += 1
            if attempts[project] == 1:
                raise ResourceDeferred('writer_proof_physical_busy')

        service.attest = attest
        self.assertTrue(service.acquire(wait=5))
        with patch.object(resident, '_failure_decision', return_value=None):
            service.start()
            for project, manifest in projects:
                svc.register(project, root=self.root)
                self.wait(lambda p=project: attempts[p] >= 1)
                state = resident.read_owner_state(manifest)
                self.assertFalse((state or {}).get('terminal', False))
                service.retry.pop(project, None)
                service.wake.set()
                self.wait(lambda m=manifest: self.ready(m))
                self.assertGreaterEqual(attempts[project], 2)
        self.assertEqual(len(self.loads), 1)

    def test_newer_service_drains_inflight_then_successor_readopts(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        old = self.service(bundle={'version': '1.0.0', 'digest': 'old'})
        self.wait(lambda: self.ready(manifest))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = svc.Worker.forward
        responses = []

        def gated(worker, *args, **kwargs):
            entered.set()
            release.wait(10)
            return original(worker, *args, **kwargs)

        with patch.object(svc.Worker, 'forward', gated):
            thread = threading.Thread(target=lambda: responses.append(embed(manifest, kind='document')))
            thread.start()
            self.assertTrue(entered.wait(5))
            spawned = []
            with patch.object(svc, 'bundle_identity',
                              return_value={'version': '1.0.1', 'digest': 'new'}):
                state = svc.ensure_service(root=self.root, learning_root=False,
                                           spawn=lambda argv, **_kwargs: spawned.append(argv))
            self.assertEqual(state, 'replacing')
            self.assertEqual(len(spawned), 1)
            self.assertTrue(old.draining.is_set())
            with self.assertRaisesRegex(IndexContractError, '^provider_unavailable$'):
                embed(manifest)
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertEqual(len(responses[0]), DIM)
        self.assertTrue(old.stopped.wait(30))
        old.close()
        self.service(bundle={'version': '1.0.1', 'digest': 'new'})
        self.wait(lambda: self.ready(manifest))
        self.assertEqual(len(embed(manifest)), DIM)
