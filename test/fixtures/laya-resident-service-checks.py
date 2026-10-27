#!/usr/bin/env python3
"""Model-free checks for the per-user Laya resident service.

A synthetic worker speaks the reviewed worker's socket protocol (one request
per connection, at most seven queued, identity envelope). The service runs in
this process; every root, socket and child process is private to the test.
"""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

from laya_index_a import IndexContractError
from resource_deferred import ResourceDeferred
import laya_embed_resident as resident
from laya_local_embed_provider import (DEFAULT_EMBED_MODEL_URI, ReviewedLocalQmdProvider,
                                       recipe_fingerprint)
import laya_resident_service as svc

DIM = 4
RUNTIME = 'qmd-2.5.3-node-llama-cpp-3.18.1'
RECIPE = 'qmd-query-format-single-document-v1'


def manifest_for(socket_path):
    digests = {'model': '1' * 64, 'provider': '2' * 64, 'worker': '3' * 64,
               'node': '4' * 64, 'entry': '5' * 64, 'closure': '6' * 64}
    fingerprint = recipe_fingerprint(
        model_id=DEFAULT_EMBED_MODEL_URI, model_sha256=digests['model'], runtime_id=RUNTIME,
        recipe_id=RECIPE, provider_sha256=digests['provider'], worker_sha256=digests['worker'],
        node_sha256=digests['node'], module_entry_sha256=digests['entry'],
        closure_sha256=digests['closure'])
    return {'approvedProviderId': 'local-qmd-synthetic', 'providerArtifactSha256': digests['provider'],
            'providerRelativePath': 'core/laya_local_embed_provider.py',
            'providerPath': '/synthetic/core/laya_local_embed_provider.py',
            'workerPath': '/synthetic/core/laya_local_embed_worker.mjs', 'workerSha256': digests['worker'],
            'serviceSocketPath': str(socket_path), 'nodePath': '/synthetic/node',
            'nodeSha256': digests['node'], 'moduleEntryPath': '/synthetic/entry.mjs',
            'moduleEntrySha256': digests['entry'], 'modelPath': '/synthetic/model.gguf',
            'modelSha256': digests['model'], 'runtimeId': RUNTIME, 'modelId': DEFAULT_EMBED_MODEL_URI,
            'embeddingFingerprint': fingerprint, 'dimension': DIM, 'recipeId': RECIPE,
            'dependencyClosureApproved': True, 'dependencyClosureSha256': digests['closure']}


class Handle:
    """Same contract as OwnedEmbedService: .process and an owned stop()."""

    def __init__(self, process, socket_path):
        self.process, self.socket_path = process, socket_path

    def stop(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lrs-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'svc'
        self.root.mkdir(mode=0o700)
        self.loads = []
        self.admit_value = True
        self.routes = {}
        self.services = []
        self.addCleanup(self.stop_services)

    def stop_services(self):
        for service in self.services:
            if not service.stopped.is_set():
                service.begin_drain()
                service.stopped.wait(10)
            service.close()

    def project(self, name):
        project = self.base / name
        project.mkdir()
        runtime = self.base / (name + '-state') / 'runtime'
        runtime.mkdir(parents=True, mode=0o700)
        runtime.parent.chmod(0o700)
        manifest = manifest_for(runtime / 'laya-embed.sock')
        decision = {'projectRoot': str(project), 'pluginRoot': '/synthetic/plugin',
                    'installId': name, 'journalPath': str(runtime / 'journal'),
                    'scopeId': name, 'dbPath': str(runtime / 'db'), 'dbIncarnation': name,
                    'bundleManifestSha256': '7' * 64, 'ownershipManifestSha256': '8' * 64,
                    'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}
        self.routes[str(project)] = (decision, {'providerManifest': manifest})
        return str(project), manifest

    def prove(self, project):
        route = self.routes.get(project)
        if route is None:
            raise IndexContractError('provider_owner_route_unavailable')
        return route[0], route[1], 'selected'

    def start_worker(self, manifest, admit, *, delay=0.05):
        if admit() is not True:
            raise IndexContractError('provider_admission_deferred')
        record = self.base / 'worker-max-pending'
        process = subprocess.Popen(
            [sys.executable, '-B', __file__, '--fake-worker', manifest['serviceSocketPath'],
             json.dumps({key: manifest[key] for key in resident._IDENTITY}),
             str(delay), str(record), str(self.base / 'slow')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if process.stdout.readline() != b'READY\n':
            raise IndexContractError('provider_start_failed')
        self.loads.append(process.pid)
        return Handle(process, manifest['serviceSocketPath'])

    def service(self, *, bundle=None, retire=None, start=True):
        service = svc.Service(self.root, bundle=bundle or {'version': '1.0.0', 'digest': 'a'},
                              prove=self.prove, attest=lambda *_a: None,
                              start_worker=self.start_worker,
                              admit=lambda: self.admit_value,
                              retire=retire or (lambda *_a: None), tick=.05)
        self.services.append(service)
        if start:
            self.assertTrue(service.acquire(wait=5))
            service.start()
        return service

    def wait(self, predicate, timeout=10, message='condition'):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(message)
            time.sleep(.02)

    def ready(self, manifest):
        try:
            return resident.probe(manifest) == 'ready'
        except Exception:
            return False


def embed(manifest, text='hello', kind='query'):
    """The reviewed provider's own socket client, unchanged."""
    provider = ReviewedLocalQmdProvider(manifest)
    return provider._embed(kind, text, timeout=2 if kind == 'query' else 30)


class ServiceChecks(Fixture):
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

    def test_bundle_replacement_finishes_in_flight_and_never_fails_a_query(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        old = self.service(bundle={'version': '1.0.0', 'digest': 'old'})
        self.wait(lambda: self.ready(manifest))
        (self.base / 'slow').write_text('1.0')
        inflight = []
        thread = threading.Thread(target=lambda: inflight.append(
            embed(manifest, 'long body', kind='document')))
        thread.start()
        self.wait(lambda: old.inflight == 1, message='request never in flight')
        spawned = []
        with patch.object(svc, 'bundle_identity', return_value={'version': '1.0.1', 'digest': 'new'}):
            state = svc.ensure_service(root=self.root, learning_root=False,
                                       spawn=lambda argv, **kw: spawned.append(argv))
        self.assertEqual(state, 'replacing')
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][3:6], [str(CORE / 'laya_resident_service.py'), 'serve',
                                           str(self.root)])
        # During the drain: absent socket -> provider_unavailable -> lexical.
        self.assertEqual(resident.probe(manifest), 'absent')
        with self.assertRaisesRegex(IndexContractError, '^provider_unavailable$'):
            embed(manifest)
        thread.join(10)
        self.assertEqual(len(inflight), 1, 'in-flight request did not finish')
        self.assertEqual(len(inflight[0]), DIM)
        self.assertTrue(old.stopped.wait(10))
        old.close()
        state_row = resident.read_owner_state(manifest)
        self.assertEqual((state_row['reason'], state_row['attempts'], state_row['terminal']),
                         ('starting', 0, False))
        self.assertTrue(resident.wait_for_release(manifest['serviceSocketPath'], timeout=3))
        # The successor takes the lease and re-adopts from the registry.
        self.service(bundle={'version': '1.0.1', 'digest': 'new'})
        self.wait(lambda: self.ready(manifest))
        self.assertEqual(len(embed(manifest)), DIM)

    def test_older_or_equal_bundle_never_drains_a_newer_service(self):
        self.service(bundle={'version': '1.0.1', 'digest': 'new'})
        spawned = []
        for version in ('1.0.0', '1.0.1'):
            with patch.object(svc, 'bundle_identity', return_value={'version': version,
                                                                    'digest': 'other'}):
                self.assertIn(svc.ensure_service(root=self.root, learning_root=False,
                                                 spawn=lambda argv, **kw: spawned.append(argv)),
                              ('ready', 'starting'))
        self.assertEqual(spawned, [])
        self.assertFalse(self.services[0].draining.is_set())

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

    def test_default_admission_uses_memory_gate_without_provider_lane(self):
        import context_learning.resource_coordination as coordination
        import context_learning.shared as shared
        calls = []

        class Controller:
            def check(self, policy, report):
                calls.append(policy)
                from context_learning.resource_coordination import decision
                return decision(policy, report)

            def activity(self, *_args):
                raise AssertionError('service must not hold the provider lane')

        def report(_policy, **_kw):
            return {'observed_at_mono': time.monotonic(), 'ready': True, 'total_mib': 16384,
                    'available_mib': 8000, 'load_per_cpu': .1,
                    'query_status': {'memory_pressure': 'ok'},
                    'memory_pressure': {'level': 'warning', 'system_free_percent': 9}}
        with patch.object(coordination, 'Coordinator', Controller), \
                patch.object(shared, 'resource_report', report):
            self.assertFalse(svc.default_admit())
        self.assertEqual(len(calls), 1)

    def test_protocol_mismatch_degrades_and_never_replaces_a_newer_service(self):
        path = self.root / svc.CONTROL
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(4)
        seen = []

        def answer():
            for _ in range(2):
                conn, _ = server.accept()
                with conn:
                    seen.append(json.loads(conn.makefile().readline()))
                    conn.sendall(json.dumps({'status': 'ready', 'protocols': [2],
                                             'bundle': {'version': '9.0.0', 'digest': 'x'}}).encode()
                                 + b'\n')
        thread = threading.Thread(target=answer)
        thread.start()
        lease = os.open(self.root / svc.LEASE, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lease, fcntl.LOCK_EX)
        try:
            spawned = []
            for _ in range(2):
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_protocol_mismatch'):
                    svc.ensure_service(root=self.root, learning_root=False,
                                       spawn=lambda argv, **kw: spawned.append(argv))
            self.assertEqual(spawned, [])
            self.assertEqual([row['command'] for row in seen], ['hello', 'hello'])
        finally:
            os.close(lease)
            thread.join(5)
            server.close()

    def test_query_hook_maps_protocol_mismatch_to_lexical_code(self):
        import laya_host_response as host
        import laya_install_journal as journal
        import runtime_work
        project, manifest = self.project('alpha')
        decision = {**self.routes[project][0], 'pluginRoot': str(CORE.parent)}
        proof = {'providerManifest': manifest, 'selectionEpoch': 1}
        resident._write_state(manifest, 'starting', attempts=0)
        stale = {**resident.read_owner_state(manifest), 'at': 0}
        cases = {'laya_service_protocol_mismatch': 'selected_provider_protocol_mismatch',
                 'laya_service_preparing': 'selected_provider_preparing',
                 'laya_service_unavailable': 'selected_provider_preparing'}
        for raised, expected in cases.items():
            with patch.object(journal, 'verify_selected_route', return_value=proof), \
                    patch.object(runtime_work, 'request', return_value={'status': 'queued'}), \
                    patch.object(resident, 'read_owner_state', return_value=stale), \
                    patch.object(resident, '_failure_decision'), \
                    patch.object(resident, 'ensure_running',
                                 side_effect=ResourceDeferred(raised)):
                self.assertEqual(host._wake_provider(decision, proof), expected)

    def test_worker_death_mid_request_is_provider_unavailable(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        service = self.service()
        self.wait(lambda: self.ready(manifest))
        (self.base / 'slow').write_text('5')
        errors = []

        def call():
            try:
                embed(manifest, 'body', kind='query')
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        thread = threading.Thread(target=call)
        thread.start()
        self.wait(lambda: service.inflight == 1)
        time.sleep(.2)
        next(iter(service.workers.values())).handle.process.kill()
        thread.join(10)
        self.assertEqual(errors, ['provider_unavailable'])

    def test_route_withdrawal_releases_lease_for_upgrade_fence(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        self.service()
        self.wait(lambda: self.ready(manifest))
        self.routes.pop(project)  # ledger 'upgrading': the route no longer proves
        self.assertTrue(resident.wait_for_release(manifest['serviceSocketPath'], timeout=5))
        self.assertEqual(resident.read_owner_state(manifest)['reason'], 'starting')
        self.routes[project] = self.routes.get(project) or self.project_route(project, manifest)
        self.wait(lambda: self.ready(manifest), message='not re-adopted after the upgrade')

    def project_route(self, project, manifest):
        name = Path(project).name
        decision = {'projectRoot': project, 'pluginRoot': '/synthetic/plugin',
                    'installId': name, 'journalPath': '/j', 'scopeId': name, 'dbPath': '/d',
                    'dbIncarnation': name + '-2', 'bundleManifestSha256': '9' * 64,
                    'ownershipManifestSha256': '8' * 64, 'vectorProfile': {}}
        return decision, {'providerManifest': manifest}

    def test_legacy_resident_is_stopped_by_exact_pid_then_adopted(self):
        project, manifest = self.project('alpha')
        other, other_manifest = self.project('beta')
        fake_core = self.base / 'old' / 'core'
        fake_core.mkdir(parents=True)
        script = fake_core / 'laya_embed_resident.py'
        script.write_text('import fcntl,os,sys,time\n'
                          'fd=os.open(sys.argv[3]+".resident.lock",os.O_CREAT|os.O_RDWR,0o600)\n'
                          'fcntl.flock(fd,fcntl.LOCK_EX)\nprint("held",flush=True)\n'
                          'time.sleep(60)\n')

        def legacy(name, socket_path):
            child = subprocess.Popen([sys.executable, '-B', str(script), 'serve', name,
                                      str(socket_path), 'a' * 64, '3'], stdout=subprocess.PIPE)
            self.assertEqual(child.stdout.readline(), b'held\n')
            self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(),
                                     child.stdout.close()))
            return child
        target = legacy(project, manifest['serviceSocketPath'])
        decoy = legacy(other, other_manifest['serviceSocketPath'])
        svc.register(project, root=self.root)
        self.service(retire=svc.retire_legacy_resident)
        self.wait(lambda: self.ready(manifest), message='legacy owner not replaced')
        self.assertEqual(target.wait(timeout=5), -15)
        self.assertIsNone(decoy.poll(), 'another installation owner was signalled')
        self.assertEqual(len(self.loads), 1)

    def test_exact_pid_match_refuses_ambiguity_and_changed_argv(self):
        argv = '/py -B /r/core/laya_embed_resident.py serve /p /s/laya-embed.sock ' + 'a' * 64 + ' 3'
        kills = []
        self.assertIsNone(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv), (12, argv)],
            inspect=lambda pid: (os.getuid(), argv), kill=lambda *a: kills.append(a)))
        self.assertIsNone(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv)],
            inspect=lambda pid: (os.getuid(), argv + ' extra'), kill=lambda *a: kills.append(a)))
        self.assertIsNone(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv.replace('/p ', '/q '))],
            inspect=lambda pid: (os.getuid(), argv), kill=lambda *a: kills.append(a)))
        self.assertEqual(kills, [])
        self.assertEqual(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv)],
            inspect=lambda pid: (os.getuid(), argv), kill=lambda *a: kills.append(a)), 11)
        self.assertEqual(len(kills), 1)


    def test_full_attestation_runs_once_per_installation_and_only_to_bind(self):
        projects = [self.project(name) for name in ('alpha', 'beta')]
        for project, _manifest in projects:
            svc.register(project, root=self.root)
        attested = []
        slow = self.start_worker

        def start_worker(manifest, admit):
            time.sleep(.5)  # many ticks pass during the cold load
            return slow(manifest, admit)
        service = self.service(start=False)
        service.attest = lambda decision, proof, mode: attested.append(decision['projectRoot'])
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        for _project, manifest in projects:
            self.wait(lambda m=manifest: self.ready(m))
        time.sleep(.3)  # further ticks re-check ownership without rehashing
        self.assertEqual(sorted(attested), sorted(p for p, _m in projects))

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


class ProofChecks(unittest.TestCase):
    def test_drifted_bundle_is_served_by_read_only_proof(self):
        import laya_install_journal as journal
        import laya_index_host_route as route
        manifest = manifest_for('/tmp/x/runtime/laya-embed.sock')
        decision = {'state': 'candidate', 'projectRoot': '/synthetic/p'}
        drift = journal.JournalEvidenceError('bundle_drift') if hasattr(
            journal, 'JournalEvidenceError') else ValueError('bundle_drift')
        with patch.object(resident, '_owned_route', side_effect=drift), \
                patch.object(journal, 'is_bundle_drift', return_value=True), \
                patch.object(route, 'select_route', return_value=decision), \
                patch.object(journal, 'verify_selected_route_read_only',
                             return_value={'readOnlyAllowed': True, 'providerManifest': manifest}):
            _decision, proof, mode = svc.default_prove('/synthetic/p')
        self.assertEqual((mode, proof['providerManifest']), ('read_only', manifest))
        for allowed, drifted, state in ((True, False, 'candidate'), (False, True, 'candidate'),
                                        (True, True, 'blocked')):
            with patch.object(resident, '_owned_route', side_effect=drift), \
                    patch.object(journal, 'is_bundle_drift', return_value=drifted), \
                    patch.object(route, 'select_route', return_value={**decision, 'state': state}), \
                    patch.object(journal, 'verify_selected_route_read_only',
                                 return_value={'readOnlyAllowed': allowed,
                                               'providerManifest': manifest}):
                with self.assertRaises(type(drift)):
                    svc.default_prove('/synthetic/p')

    def test_full_attestation_applies_to_selected_routes_only(self):
        import laya_install_journal as journal
        manifest = manifest_for('/tmp/x/runtime/laya-embed.sock')
        proof = {'providerManifest': manifest}
        with patch.object(journal, 'verify_worker_attestation', return_value=manifest) as verify:
            svc.default_attest({}, proof, 'selected')
            svc.default_attest({}, proof, 'preview')
            svc.default_attest({}, proof, 'read_only')
        self.assertEqual(verify.call_count, 1)
        with patch.object(journal, 'verify_worker_attestation', return_value={**manifest, 'x': 1}):
            with self.assertRaisesRegex(IndexContractError, 'provider_attestation_invalid'):
                svc.default_attest({}, proof, 'selected')


class ClientChecks(Fixture):
    def setUp(self):
        super().setUp()
        # Never resolve the account's real learning root from a fixture.
        learning = patch.object(svc, '_learning_root', return_value=None)
        learning.start()
        self.addCleanup(learning.stop)

    def test_two_projects_request_one_service_spawn(self):
        spawned = []
        for name in ('alpha', 'beta'):
            _project, manifest = self.project(name)
            with patch.object(resident, '_failure_decision'):
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                    resident.ensure_running(_project, manifest, service_root=self.root,
                                            spawn=lambda argv, **kw: spawned.append(argv))
            # A waiting successor holds its lock: model it after the first spawn.
            if spawned and not hasattr(self, 'held'):
                self.held = os.open(self.root / svc.SUCCESSOR, os.O_RDWR)
                fcntl.flock(self.held, fcntl.LOCK_EX)
                self.addCleanup(os.close, self.held)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][3:6], [str(CORE / 'laya_resident_service.py'), 'serve',
                                           str(self.root)])
        self.assertEqual(len(self.service(start=False).registered()), 2)

    def test_fixture_and_sandbox_never_spawn(self):
        _project, manifest = self.project('alpha')
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1'}), \
                patch.object(resident, '_failure_decision'):
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_fixture'):
                resident.ensure_running(_project, manifest)

    def test_version_order(self):
        self.assertTrue(svc.newer({'version': '0.38.2'}, {'version': '0.38.1'}))
        self.assertTrue(svc.newer({'version': '0.38.1'}, {'version': '0.38.1-dev.4'}))
        self.assertTrue(svc.newer({'version': '0.10.0'}, {'version': '0.9.9'}))
        self.assertFalse(svc.newer({'version': '0.38.1'}, {'version': '0.38.1'}))
        self.assertFalse(svc.newer({'version': '0.38.0'}, {'version': '0.38.1'}))


class LearningChecks(unittest.TestCase):
    def test_learning_inference_is_reached_through_the_resident_service(self):
        from context_learning import inference_service as learning
        with tempfile.TemporaryDirectory(prefix='lrl-', dir='/tmp') as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            stop = threading.Event()
            calls = []

            def host(**kwargs):
                calls.append(kwargs)
                engine = learning.GenerationEngine(
                    load=lambda job: 'loaded',
                    forward=lambda loaded, request, **kw: {'head': loaded},
                    admission=lambda *_a, **_k: ({}, {}), rss=lambda: 100)
                threading.Thread(target=learning._serve, args=(root,),
                                 kwargs={'engine': engine, 'stop': stop, 'hosted': True},
                                 daemon=True).start()
                return 'starting'
            try:
                with patch.object(svc, 'ensure_service', side_effect=host):
                    learning._ensure(root, sys.executable, time.monotonic() + 5)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], {'python': sys.executable, 'learning_root': root})
                answer = learning._version(root, time.monotonic() + 1)
                self.assertEqual((answer['hosted'], answer['python']), (True, sys.executable))
                # Older clients cannot shut a hosted service down.
                reply = learning._exchange(root, {'command': 'shutdown'}, time.monotonic() + 1)
                self.assertEqual(reply['status'], 'busy')
                self.assertTrue(learning._ready(root, time.monotonic() + 1))
            finally:
                stop.set()
                deadline = time.monotonic() + 5
                while (root / learning.SOCKET_NAME).exists() and time.monotonic() < deadline:
                    time.sleep(.02)

    def test_learning_client_needing_its_interpreter_rebases_same_release(self):
        answer = {'status': 'ready', 'protocols': [1], 'python': '/other/python',
                  'bundle': svc.bundle_identity()}
        with tempfile.TemporaryDirectory(prefix='lrp-', dir='/tmp') as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            sent, spawned = [], []

            def exchange(_root, row, **_kw):
                sent.append(row['command'])
                return answer if row['command'] == 'hello' else {'status': 'draining'}
            with patch.object(svc, 'exchange', side_effect=exchange):
                self.assertEqual(svc.ensure_service(root=root, python=sys.executable,
                                                    learning_root=False,
                                                    spawn=lambda argv, **kw: spawned.append(argv)),
                                 'replacing')
            self.assertEqual(sent, ['hello', 'drain'])
            self.assertEqual(spawned[0][:3], [sys.executable, '-I', '-B'])
            self.assertEqual(json.loads((root / svc.INTERPRETER).read_text())['python'],
                             sys.executable)


def fake_worker(socket_path, identity, delay, record, slow):
    identity = json.loads(identity)
    delay = float(delay)
    lock = threading.Lock()
    state = {'pending': 0, 'max': 0}
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(16)

    def handle(conn):
        with conn:
            data = bytearray()
            while True:
                block = conn.recv(65536)
                if not block:
                    break
                data.extend(block)
            request = json.loads(data)
            if request == {'kind': 'status'}:
                conn.sendall(json.dumps({**identity, 'status': 'ready'}).encode())
                return
            with lock:
                if state['pending'] >= 7:
                    conn.sendall(json.dumps({**identity, 'ok': False, 'result': None}).encode())
                    return
                state['pending'] += 1
                state['max'] = max(state['max'], state['pending'])
                Path(record).write_text(str(state['max']))
            try:
                pause = delay
                if os.path.exists(slow):
                    pause = float(Path(slow).read_text())
                time.sleep(pause)
                conn.sendall(json.dumps({**identity, 'ok': True,
                                         'result': {'vector': [0.5] * DIM}}).encode())
            finally:
                with lock:
                    state['pending'] -= 1

    def serve():
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    sys.stdout.write('READY\n')
    sys.stdout.flush()
    sys.stdin.read()  # EOF: the owner died or stopped this worker
    os._exit(0)


if __name__ == '__main__':
    if len(sys.argv) == 7 and sys.argv[1] == '--fake-worker':
        fake_worker(*sys.argv[2:])
    else:
        unittest.main()
