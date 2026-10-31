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
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
# Last release under the earlier plugin id (its own resident client code).
OLD_RELEASE = 'f9c33094e30d523415fe983ec7f3b4c35d5a21c0'
sys.path.insert(0, str(CORE))

from laya_index_a import IndexContractError
from resource_deferred import ResourceDeferred
import laya_embed_resident as resident
from laya_local_embed_provider import (DEFAULT_EMBED_MODEL_URI, ReviewedLocalQmdProvider,
                                       recipe_fingerprint)
import laya_resident_service as svc

DIM = 4
TIME_SCALE = .02  # subprocess drain checks: budget 9.6 s, successor wait 12 s
RUNTIME = 'qmd-2.5.3-node-llama-cpp-3.18.1'
RECIPE = 'qmd-query-format-single-document-v1'


def manifest_for(socket_path, *, model='1', dimension=DIM):
    digests = {'model': model * 64, 'provider': '2' * 64, 'worker': '3' * 64,
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
            'embeddingFingerprint': fingerprint, 'dimension': dimension, 'recipeId': RECIPE,
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
        self.modes = {}
        self.services = []
        self.addCleanup(self.stop_services)

    def stop_services(self):
        for service in self.services:
            if not service.stopped.is_set():
                service.begin_drain()
                service.stopped.wait(10)
            service.close()

    def project(self, name, **variant):
        project = self.base / name
        project.mkdir()
        runtime = self.base / (name + '-state') / 'runtime'
        runtime.mkdir(parents=True, mode=0o700)
        runtime.parent.chmod(0o700)
        manifest = manifest_for(runtime / 'laya-embed.sock', **variant)
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
        return route[0], route[1], self.modes.get(project, 'selected')

    def start_worker(self, manifest, admit, *, delay=0.05):
        if admit() is not True:
            raise IndexContractError('provider_admission_deferred')
        record = self.base / 'worker-max-pending'
        process = subprocess.Popen(
            [sys.executable, '-B', __file__, '--fake-worker', manifest['serviceSocketPath'],
             json.dumps({key: manifest[key] for key in resident._IDENTITY}),
             str(delay), str(record), str(self.base / 'slow'), str(manifest['dimension'])],
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

    def old_client(self, legacy):
        """One 0.40.3 client request, run by the 0.40.3 code itself (git history)."""
        if not hasattr(self, 'old_core'):
            import io
            import tarfile
            archive = subprocess.run(('git', '-C', str(CORE.parent), 'archive', '--format=tar',
                                      OLD_RELEASE, 'core'), check=True,
                                     capture_output=True).stdout
            target = self.base / 'old-0.40.3'
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(target, filter='data')
            self.old_core = target / 'core'
        script = ('import json, sys\n'
                  'sys.path.insert(0, sys.argv[1])\n'
                  'import laya_resident_service as s\n'
                  'spawned = []\n'
                  'state = s.ensure_service(root=sys.argv[2], learning_root=False,\n'
                  '                         spawn=lambda argv, **kw: spawned.append(argv))\n'
                  'print(json.dumps({"state": state, "spawned": len(spawned)}))\n')
        for _attempt in range(3):
            done = subprocess.run((sys.executable, '-B', '-c', script, str(self.old_core),
                                   str(legacy)), capture_output=True, text=True, check=False,
                                  env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            # The 0.40.3 client gives its hello a fixed 0.5 s; on a loaded
            # host it reports laya_service_busy before the in-process service
            # answers. That is the old code's deadline, not the state asked
            # about here: ask again, a bounded number of times.
            if done.returncode == 0 or 'laya_service_busy' not in done.stderr:
                break
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_handoff_elects_only_the_memvane_successor_against_a_0403_client(self):
        legacy = self.base / 'legacy-svc'
        legacy.mkdir(mode=0o700)
        project, manifest = self.project('alpha')
        svc.register(project, root=legacy)
        old = svc.Service(legacy, bundle={'version': '0.40.3', 'digest': 'old'},
                          prove=self.prove, attest=lambda *_a: None,
                          start_worker=self.start_worker, admit=lambda: True,
                          retire=lambda *_a: None, tick=.05, legacy_root=None)
        self.services.append(old)
        self.assertTrue(old.acquire(wait=5))
        old.start()
        self.wait(lambda: self.ready(manifest))
        inherited = []

        def spawn(argv, *, pass_fds):  # the child keeps its copies of the locks
            inherited.append((argv, [os.dup(fd) for fd in pass_fds]))
        with patch.object(svc, 'bundle_identity',
                          return_value={'version': '0.41.0', 'digest': 'new'}):
            self.assertEqual(svc.ensure_service(root=self.root, learning_root=False,
                                                legacy_root=legacy, spawn=spawn), 'replacing')
        self.assertTrue(old.draining.is_set())
        self.assertEqual(len(inherited), 1)  # the MemVane successor, started first
        argv, fds = inherited[0]
        self.assertEqual(argv[3:6], [str(CORE / 'laya_resident_service.py'), 'serve',
                                     str(self.root)])
        self.assertEqual(len(fds), 2)
        # A 0.40.3 client asks during the drain: its own code elects no successor.
        self.assertEqual(self.old_client(legacy), {'state': 'replacing', 'spawned': 0})
        new = svc.Service(self.root, bundle={'version': '0.41.0', 'digest': 'new'},
                          prove=self.prove, attest=lambda *_a: None,
                          start_worker=self.start_worker, admit=lambda: True,
                          retire=lambda *_a: None, tick=.05, legacy_root=legacy)
        self.services.append(new)
        acquired = []
        thread = threading.Thread(target=lambda: acquired.append(new.acquire(
            successor_fd=fds[0], wait=20, legacy_successor_fd=fds[1])))
        thread.start()
        self.assertTrue(old.stopped.wait(15))
        old.close()
        thread.join(20)
        self.assertEqual(acquired, [True])
        self.assertIsNotNone(new.legacy_lease)
        # The pre-rename service stays down: later 0.40.3 requests elect nothing.
        self.assertEqual(self.old_client(legacy), {'state': 'starting', 'spawned': 0})
        self.assertIsNone(svc.legacy_service_dir(home=self.base / 'nohome'))

    def test_memvane_service_holds_the_pre_rename_lease_for_its_lifetime(self):
        legacy = self.base / 'legacy-svc'
        new = svc.Service(self.root, bundle={'version': '0.41.0', 'digest': 'new'},
                          prove=self.prove, attest=lambda *_a: None,
                          start_worker=self.start_worker, admit=lambda: True,
                          retire=lambda *_a: None, tick=.05, legacy_root=legacy)
        self.services.append(new)
        self.assertTrue(new.acquire(wait=5))  # no pre-rename directory: nothing held
        new.start()
        self.assertIsNone(new.legacy_lease)
        legacy.mkdir(mode=0o700)  # a 0.40.3 client created its service directory
        self.wait(lambda: new.legacy_lease is not None, message='legacy lease not taken')
        # A 0.40.3-style successor never gets the lease while MemVane runs ...
        old = svc.Service(legacy, bundle={'version': '0.40.3', 'digest': 'old'},
                          prove=self.prove, attest=lambda *_a: None,
                          start_worker=self.start_worker, admit=lambda: True,
                          retire=lambda *_a: None, tick=.05, legacy_root=None)
        self.services.append(old)
        self.assertFalse(old.acquire(wait=.5))
        # ... and a 0.40.3-style client sees a held lease: no spawn, it degrades.
        spawned = []
        with patch.object(svc, 'bundle_identity',
                          return_value={'version': '0.40.3', 'digest': 'old'}):
            self.assertEqual(svc.ensure_service(root=legacy, learning_root=False,
                                                legacy_root=None,
                                                spawn=lambda argv, **kw: spawned.append(argv)),
                             'starting')
        self.assertEqual(spawned, [])
        # The MemVane client attaches to the running MemVane service.
        with patch.object(svc, 'bundle_identity',
                          return_value={'version': '0.41.0', 'digest': 'new'}):
            self.assertIn(svc.ensure_service(root=self.root, learning_root=False,
                                             legacy_root=legacy,
                                             spawn=lambda argv, **kw: spawned.append(argv)),
                          ('ready', 'starting'))
        self.assertEqual(spawned, [])
        new.begin_drain()
        self.assertTrue(new.stopped.wait(10))
        new.close()
        self.assertTrue(old.acquire(wait=2))  # released only when MemVane stops

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

    def project_route(self, project, manifest):
        name = Path(project).name
        decision = {'projectRoot': project, 'pluginRoot': '/synthetic/plugin',
                    'installId': name, 'journalPath': '/j', 'scopeId': name, 'dbPath': '/d',
                    'dbIncarnation': name + '-2', 'bundleManifestSha256': '9' * 64,
                    'ownershipManifestSha256': '8' * 64, 'vectorProfile': {}}
        return decision, {'providerManifest': manifest}

    def test_legacy_resident_is_stopped_by_exact_pid_then_adopted(self):
        self.legacy_resident_replaced('alpha', 'beta')

    def test_legacy_resident_of_a_spaced_project_path_is_matched_by_real_argv(self):
        # A ps command string joins argv with spaces: such a project never matched.
        self.legacy_resident_replaced('막차 뒤의 피난소', '막차 뒤의')

    def legacy_resident_replaced(self, name, other_name):
        project, manifest = self.project(name)
        other, other_manifest = self.project(other_name)
        fake_core = self.base / 'old copy' / 'core'
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
        argv = ('/usr/bin/python3', ['/py', '-B', '/r/core/laya_embed_resident.py', 'serve',
                                     '/p', '/s/laya-embed.sock', 'a' * 64, '3'])
        extra = (argv[0], argv[1] + ['extra'])
        moved = (argv[0], [part.replace('/p', '/q') if part == '/p' else part
                           for part in argv[1]])
        not_python = ('/bin/sh', argv[1])
        kills = []
        self.assertIsNone(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv), (12, argv)],
            inspect=lambda pid: (os.getuid(), argv), kill=lambda *a: kills.append(a)))
        self.assertIsNone(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv)],
            inspect=lambda pid: (os.getuid(), extra), kill=lambda *a: kills.append(a)))
        for row in (moved, not_python):
            self.assertIsNone(svc.retire_legacy_resident(
                '/p', '/s/laya-embed.sock', rows=[(11, row)],
                inspect=lambda pid, row=row: (os.getuid(), row),
                kill=lambda *a: kills.append(a)))
        self.assertEqual(kills, [])
        self.assertEqual(svc.retire_legacy_resident(
            '/p', '/s/laya-embed.sock', rows=[(11, argv)],
            inspect=lambda pid: (os.getuid(), argv), kill=lambda *a: kills.append(a)), 11)
        self.assertEqual(len(kills), 1)
        # Real argv boundaries: one argument may hold spaces.
        spaced = (argv[0], argv[1][:4] + ['/w/막차 뒤의 피난소'] + argv[1][5:])
        self.assertEqual(svc.retire_legacy_resident(
            '/w/막차 뒤의 피난소', '/s/laya-embed.sock', rows=[(12, spaced)],
            inspect=lambda pid: (os.getuid(), spaced), kill=lambda *a: kills.append(a)), 12)
        self.assertIsNone(svc.retire_legacy_resident(
            '/w/막차', '/s/laya-embed.sock', rows=[(12, spaced)],
            inspect=lambda pid: (os.getuid(), spaced), kill=lambda *a: kills.append(a)))

    def test_process_argv_reads_real_boundaries_of_a_live_child(self):
        code = 'import sys, time; print("READY", flush=True); time.sleep(30)'
        child = subprocess.Popen([sys.executable, '-B', '-c', code, 'a b', '막차 뒤의 피난소'],
                                 stdout=subprocess.PIPE)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(),
                                 child.stdout.close()))
        # Inspect only after the interpreter runs the code: during exec the
        # kernel may not expose the final argv yet.
        readable, _, _ = select.select([child.stdout], [], [], 5)
        self.assertEqual(readable, [child.stdout], 'child never reported readiness')
        self.assertEqual(child.stdout.readline(), b'READY\n')
        found = svc._process_argv(child.pid)
        self.assertIsNotNone(found)
        executable, argv = found
        self.assertIn('python', os.path.basename(executable).lower())
        self.assertEqual(argv[-2:], ['a b', '막차 뒤의 피난소'])
        self.assertEqual(argv[1:4], ['-B', '-c', code])
        self.assertIn(child.pid, dict(svc._process_rows(os.getuid())))


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


class ServiceBoundsChecks(Fixture):
    def test_read_only_proof_attaches_but_never_starts_a_worker(self):
        drifted, drifted_manifest = self.project('alpha')
        self.modes[drifted] = 'read_only'
        svc.register(drifted, root=self.root)
        service = self.service()
        time.sleep(.5)  # many ticks: a drifted bundle must not cold-load the model
        self.assertEqual(self.loads, [])
        self.assertEqual(resident.probe(drifted_manifest), 'absent')
        # A fully proven installation of the same profile starts the worker;
        # the read-only one then only attaches to it.
        selected, selected_manifest = self.project('beta')
        svc.register(selected, root=self.root)
        service.retry.pop(drifted, None)
        self.wait(lambda: self.ready(selected_manifest))
        service.retry.pop(drifted, None)
        self.wait(lambda: self.ready(drifted_manifest), message='read-only never attached')
        self.assertEqual(len(self.loads), 1)

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
        next(iter(service.workers.values())).handle.process.kill()
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

    def test_cold_loads_of_different_profiles_never_overlap(self):
        alpha, alpha_manifest = self.project('alpha')
        beta, beta_manifest = self.project('beta', model='9')
        self.assertNotEqual(alpha_manifest['embeddingFingerprint'],
                            beta_manifest['embeddingFingerprint'])
        for project in (alpha, beta):
            svc.register(project, root=self.root)
        state = {'now': 0, 'max': 0}
        lock = threading.Lock()
        start = self.start_worker

        def start_worker(manifest, admit):
            with lock:
                state['now'] += 1
                state['max'] = max(state['max'], state['now'])
            try:
                time.sleep(.4)  # one admission check, then a slow model load
                return start(manifest, admit)
            finally:
                with lock:
                    state['now'] -= 1
        service = self.service(start=False)
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        for manifest in (alpha_manifest, beta_manifest):
            self.wait(lambda m=manifest: self.ready(m))
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(state['max'], 1, 'two cold loads passed admission together')

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
        time.sleep(.3)
        self.assertIsNone(handle.process.poll(), 'a worker still bound was stopped')
        self.routes.pop(beta)
        self.wait(lambda: handle.process.poll() is not None, message='unused worker kept')
        self.wait(lambda: service.workers == {}, message='worker entry kept')

    def test_connection_cap_bounds_handlers_and_falls_back_to_lexical(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        with patch.object(svc, 'MAX_CONNECTIONS', 2, create=True), \
                patch.object(svc, 'LISTEN_BACKLOG', 2, create=True):
            self.service()
            self.wait(lambda: self.ready(manifest))
            idle = []
            for _ in range(12):
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.settimeout(.2)
                try:
                    conn.connect(manifest['serviceSocketPath'])
                except OSError:
                    conn.close()
                    continue
                idle.append(conn)
            time.sleep(.5)
            handlers = [t for t in threading.enumerate() if t.name == 'laya-service-client']
            self.assertLessEqual(len(handlers), 2, 'a handler thread per connection')
            with self.assertRaisesRegex(IndexContractError, '^provider_unavailable$'):
                embed(manifest)
            for conn in idle:
                conn.close()
            self.wait(lambda: not [t for t in threading.enumerate()
                                   if t.name == 'laya-service-client'])
            self.assertEqual(len(embed(manifest)), DIM)

    def test_idle_listeners_hold_no_connection_slot(self):
        manifests = []
        for name in ('alpha', 'beta', 'gamma', 'delta'):
            project, manifest = self.project(name)
            svc.register(project, root=self.root)
            manifests.append(manifest)
        with patch.object(svc, 'MAX_CONNECTIONS', 4, create=True):
            service = self.service()
            for manifest in manifests:
                self.wait(lambda m=manifest: self.ready(m))
            # The last readiness probe's handler returns its slot just after.
            self.wait(lambda: service.connections._value == 4)
            free = []
            for _ in range(10):
                free.append(service.connections._value)
                time.sleep(.05)
        self.assertEqual(free, [4] * 10, 'idle accept loops took connection slots')

    def test_drain_waits_for_an_in_progress_load_and_stops_its_model(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        events, handles = [], []
        entered = threading.Event()
        start = self.start_worker

        def start_worker(manifest, admit):
            entered.set()
            time.sleep(1)  # the model is still loading when the drain begins
            handle = start(manifest, admit)
            handles.append(handle)
            events.append('returned')
            return handle
        service = self.service(start=False)
        service.start_worker = start_worker
        self.assertTrue(service.acquire(wait=5))
        service.start()
        self.assertTrue(entered.wait(5))
        threading.Thread(target=lambda: service.stopped.wait(30) and events.append(
            ('stopped', handles[0].process.poll() is not None if handles else None)),
            daemon=True).start()
        service.begin_drain()
        self.assertTrue(service.stopped.wait(30))
        self.wait(lambda: len(events) == 2)
        # The lease is released only after the load settled and its model stopped.
        self.assertEqual(events, ['returned', ('stopped', True)])

    def test_drain_keeps_the_lease_past_its_wait_interval_while_a_load_runs(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        events, handles = [], []
        entered = threading.Event()
        start = self.start_worker

        def start_worker(manifest, admit):
            entered.set()
            time.sleep(1.5)  # longer than several drain wait intervals
            handle = start(manifest, admit)
            handles.append(handle)
            events.append('returned')
            return handle
        with patch.object(svc, 'LOAD_DRAIN_SECONDS', .2, create=True):
            service = self.service(start=False)
            service.start_worker = start_worker
            self.assertTrue(service.acquire(wait=5))
            service.start()
            self.assertTrue(entered.wait(5))
            threading.Thread(target=lambda: service.stopped.wait(30) and events.append(
                ('stopped', handles[0].process.poll() is not None if handles else None)),
                daemon=True).start()
            service.begin_drain()
            self.assertTrue(service.stopped.wait(30))
            self.wait(lambda: len(events) == 2)
        self.assertEqual(events, ['returned', ('stopped', True)])

    def test_drain_kills_a_worker_whose_stop_never_returns(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        hung = threading.Event()
        self.addCleanup(hung.set)
        start = self.start_worker

        class Stuck(Handle):
            def stop(self):
                hung.wait(60)  # a stop that never finishes on its own
        handles = []

        def start_worker(manifest, admit):
            handle = start(manifest, admit)
            handles.append(Stuck(handle.process, handle.socket_path))
            return handles[-1]
        with patch.object(svc, 'MAX_EXIT_CLEANUP_SECONDS', 1, create=True):
            service = self.service(start=False)
            service.start_worker = start_worker
            self.assertTrue(service.acquire(wait=5))
            service.start()
            self.wait(lambda: self.ready(manifest))
            began = time.monotonic()
            service.begin_drain()
            self.assertTrue(service.stopped.wait(10))
        self.assertLess(time.monotonic() - began, 5, 'exit cleanup ignored its bound')
        self.wait(lambda: handles[0].process.poll() is not None,
                  message='stuck worker left running')
        handles[0].process.stdout.close()

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

    def test_listener_fd_above_fd_setsize_still_accepts(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = 1400
        if hard != resource.RLIM_INFINITY and hard < wanted:
            self.skipTest('hard descriptor limit below FD_SETSIZE')
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        spare = []
        self.addCleanup(lambda: [os.close(fd) for fd in spare])
        null = os.open(os.devnull, os.O_RDONLY)
        spare.append(null)
        while max(spare) < 1100:
            spare.append(os.dup(null))
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        self.service()
        self.wait(lambda: self.ready(manifest), message='listener above FD_SETSIZE never served')
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
            time.sleep(.3)
            self.assertIsNone(handle.process.poll(), 'worker reclaimed under a request')
            release.set()
            thread.join(30)
        self.assertEqual(len(results), 1)
        self.wait(lambda: handle.process.poll() is not None, message='never reclaimed after')

    def test_slow_sender_is_cut_at_one_absolute_read_deadline(self):
        project, manifest = self.project('alpha')
        svc.register(project, root=self.root)
        with patch.object(svc, 'READ_SECONDS', 1, create=True):
            self.service()
            self.wait(lambda: self.ready(manifest))
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(conn.close)
            conn.connect(manifest['serviceSocketPath'])
            started = time.monotonic()
            closed = None
            while time.monotonic() - started < 4:
                try:
                    conn.sendall(b' ')  # one byte at a time, never the whole request
                except OSError:
                    closed = time.monotonic() - started
                    break
                time.sleep(.1)
            self.assertIsNotNone(closed, 'a trickling sender kept its connection')
            self.assertLess(closed, 3)


class HungLoadChecks(unittest.TestCase):
    def test_drain_past_a_hung_load_kills_its_children_and_exits(self):
        temp = tempfile.TemporaryDirectory(prefix='lrh-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        root = base / 'svc'
        root.mkdir(mode=0o700)
        child_record = base / 'child.pid'
        env = {**os.environ, 'HOME': str(base), 'QMD_SETUP_GUARD_FIXTURE': '1',
               'LAYA_SERVICE_TIME_SCALE': str(TIME_SCALE), 'PYTHONDONTWRITEBYTECODE': '1'}
        driver = subprocess.Popen([sys.executable, '-B', __file__, '--hung-load-service',
                                   str(base), str(child_record)],
                                  env=env, stdin=subprocess.DEVNULL, start_new_session=True)

        def cleanup():
            for pid in ([driver.pid] if driver.poll() is None else []) + (
                    [int(child_record.read_text())] if child_record.exists() else []):
                with suppress_lookup():
                    os.kill(pid, 9)
            driver.wait(5)
        self.addCleanup(cleanup)
        try:
            code = driver.wait(30)
        except subprocess.TimeoutExpired:
            self.fail('a drain behind a hung load never exited')
        self.assertEqual(code, svc.FORCED_EXIT_CODE)
        child = int(child_record.read_text())
        with self.assertRaises(ProcessLookupError, msg='orphan embed child left running'):
            os.kill(child, 0)
        record = json.loads((root / svc.FORCED_EXIT).read_text())
        self.assertEqual((record['reason'], record['unconfirmedChildren']), ('cold_load_hung', []))
        self.assertIn(child, record['killedChildren'])
        # The kernel released the lease with the process: a successor takes it.
        successor = svc.Service(root, bundle={'version': '1.0.1', 'digest': 'n'},
                                prove=lambda _p: None)
        self.addCleanup(successor.close)
        self.assertTrue(successor.acquire(wait=2))


    def test_successor_waiting_from_drain_start_takes_the_lease(self):
        # A busy learning phase, then a hung cold load: every drain phase fits
        # one budget below the successor's own lease wait.
        temp = tempfile.TemporaryDirectory(prefix='lrb-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        (base / 'svc').mkdir(mode=0o700)
        child_record, draining = base / 'child.pid', base / 'draining'
        scale = TIME_SCALE
        env = {**os.environ, 'HOME': str(base), 'QMD_SETUP_GUARD_FIXTURE': '1',
               'LAYA_SERVICE_TIME_SCALE': str(scale),
               # Read only by a service without the scaled budget (before the fix).
               'LAYA_SERVICE_LOAD_STUCK_SECONDS': str(300 * scale),
               'PYTHONDONTWRITEBYTECODE': '1'}
        spawned = []

        def spawn(*argv):
            child = subprocess.Popen([sys.executable, '-B', __file__, *argv], env=env,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            spawned.append(child)
            return child

        def cleanup():
            for child in spawned:
                if child.poll() is None:
                    child.kill()
                child.wait(5)
            if child_record.exists():
                with suppress_lookup():
                    os.kill(int(child_record.read_text()), 9)
        self.addCleanup(cleanup)
        old = spawn('--hung-load-service', str(base), str(child_record), 'learning-busy')
        deadline = time.monotonic() + 15
        while not draining.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(draining.exists(), 'drain never began')
        successor = spawn('--successor', str(base / 'svc'),
                          str(svc.SUCCESSOR_WAIT_SECONDS * scale))
        self.assertEqual(successor.wait(30), 0, 'the successor gave up on the lease')
        self.assertEqual(old.wait(10), svc.FORCED_EXIT_CODE)
        with self.assertRaises(ProcessLookupError, msg='orphan embed child left running'):
            os.kill(int(child_record.read_text()), 0)

    def test_drain_budget_and_exit_cleanup_stay_below_the_successor_wait(self):
        def total():
            # Every drain phase, then the exit cleanup bound, then the safety gap.
            return (svc._scaled(svc.DRAIN_BUDGET_SECONDS) + svc._exit_cleanup_seconds()
                    + svc._scaled(svc.DRAIN_SAFETY_SECONDS))
        self.assertLessEqual(svc.DRAIN_SECONDS, svc.DRAIN_BUDGET_SECONDS)
        self.assertLessEqual(total(), svc.SUCCESSOR_WAIT_SECONDS)
        # The scale the subprocess checks use keeps the same inequality, with
        # the cleanup floor that a scaled test does not shrink.
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1',
                                     'LAYA_SERVICE_TIME_SCALE': str(TIME_SCALE)}):
            self.assertLessEqual(total(), svc._scaled(svc.SUCCESSOR_WAIT_SECONDS))
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '',
                                     'LAYA_SERVICE_TIME_SCALE': '0.01'}):
            self.assertEqual(svc._scaled(svc.DRAIN_BUDGET_SECONDS), svc.DRAIN_BUDGET_SECONDS)


class suppress_lookup:
    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _tb):
        return kind is ProcessLookupError


def successor(root, wait):
    """A replacement that waits for the lease from drain start; 0 when it got it."""
    service = svc.Service(Path(root), bundle={'version': '1.0.1', 'digest': 'n'},
                          prove=lambda _p: None)
    acquired = service.acquire(wait=float(wait))
    service.close()
    raise SystemExit(0 if acquired else 75)


def hung_load_service(base, child_record, phase=''):
    """A service whose only cold load spawns its model child, then hangs forever."""
    base, child_record = Path(base), Path(child_record)
    root = base / 'svc'
    project = base / 'alpha'
    project.mkdir()
    runtime = base / 'alpha-state' / 'runtime'
    runtime.mkdir(parents=True, mode=0o700)
    runtime.parent.chmod(0o700)
    manifest = manifest_for(runtime / 'laya-embed.sock')
    decision = {'projectRoot': str(project), 'pluginRoot': '/synthetic/plugin',
                'installId': 'alpha', 'journalPath': str(runtime / 'journal'),
                'scopeId': 'alpha', 'dbPath': str(runtime / 'db'), 'dbIncarnation': 'alpha',
                'bundleManifestSha256': '7' * 64, 'ownershipManifestSha256': '8' * 64,
                'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}

    def start_worker(_manifest, _admit):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'],
                                 stdin=subprocess.DEVNULL)
        child_record.write_text(str(child.pid))
        threading.Event().wait()  # never READY, never returns
    svc.LOAD_DRAIN_SECONDS = .2
    if not hasattr(svc, '_scaled'):
        # A service before the shared budget reads no time scale: same scaled values.
        svc.DRAIN_SECONDS = 330 * float(os.environ.get('LAYA_SERVICE_TIME_SCALE', 1))
    svc.register(str(project), root=root)
    service = svc.Service(root, bundle={'version': '1.0.0', 'digest': 'a'},
                          learning_root=str(base / 'learning') if phase else None,
                          prove=lambda _p: (decision, {'providerManifest': manifest},
                                            'selected'),
                          attest=lambda *_a: None, start_worker=start_worker,
                          admit=lambda: True, retire=lambda *_a: None, tick=.05)
    if not service.acquire(wait=5):
        raise SystemExit(75)
    if phase == 'learning-busy':
        service._learning_busy = lambda: True  # a learning request never finishes
    service.start()
    deadline = time.monotonic() + 10
    while not child_record.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    service.begin_drain()
    (base / 'draining').write_text('1')
    service.run()


class DeferredStartChecks(unittest.TestCase):
    """A resource-deferred embed start never re-hashes the runtime."""
    def test_admission_is_checked_before_the_full_attestation(self):
        with tempfile.TemporaryDirectory(prefix='lrs-defer-', dir='/tmp') as temp:
            base = Path(temp).resolve(); base.chmod(0o700)
            root = base / 'svc'; root.mkdir(mode=0o700)
            runtime = base / 'state' / 'runtime'; runtime.mkdir(parents=True, mode=0o700)
            runtime.parent.chmod(0o700)
            manifest = manifest_for(runtime / 'laya-embed.sock')
            decision = {'projectRoot': str(base / 'p')}
            attests, starts, admitted = [], [], [False]
            def start_worker(_manifest, admit):
                starts.append(1)
                return object() if admit() else (_ for _ in ()).throw(
                    ResourceDeferred('provider_admission_deferred'))
            service = svc.Service(root, bundle={'version': '1.0.0', 'digest': 'a'},
                                  prove=lambda _p: (decision, {'providerManifest': manifest}, 'selected'),
                                  attest=lambda *_a: attests.append(1), start_worker=start_worker,
                                  admit=lambda: admitted[0], retire=lambda *_a: None, tick=.05)
            worker = svc.Worker('key', str(root / 'w.sock'))
            for _ in range(3):
                worker.loading, worker.retry_at = True, 0.0
                service._load_attested(str(base / 'p'), decision, {'providerManifest': manifest},
                                       'selected', manifest, worker, 'seal')
                self.assertEqual(worker.state, 'resource_deferred')
            self.assertEqual((attests, starts), ([], []), 'deferred retries re-attested')
            admitted[0] = True
            worker.loading, worker.retry_at = True, 0.0
            service._load_attested(str(base / 'p'), decision, {'providerManifest': manifest},
                                   'selected', manifest, worker, 'seal')
            self.assertEqual((len(attests), len(starts), worker.state), (1, 1, 'ready'))


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
        decision = {'journalPath': '/tmp/x/install-journal.sqlite'}
        with patch.object(journal, 'verify_worker_attestation', return_value=manifest) as verify, \
                patch.object(journal, 'verify_preview_worker_attestation',
                             return_value=manifest) as preview:
            svc.default_attest(decision, proof, 'selected')
            svc.default_attest(decision, proof, 'preview')
            svc.default_attest(decision, proof, 'read_only')
        self.assertEqual(verify.call_count, 1)
        # A pending build's worker start re-hashes its runtime files too.
        self.assertEqual(preview.call_args.args, (decision['journalPath'], manifest))
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


def fake_worker(socket_path, identity, delay, record, slow, dimension):
    identity = json.loads(identity)
    delay = float(delay)
    dimension = int(dimension)
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
                                         'result': {'vector': [0.5] * dimension}}).encode())
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
    if len(sys.argv) == 8 and sys.argv[1] == '--fake-worker':
        fake_worker(*sys.argv[2:])
    elif len(sys.argv) in (4, 5) and sys.argv[1] == '--hung-load-service':
        hung_load_service(*sys.argv[2:])
    elif len(sys.argv) == 4 and sys.argv[1] == '--successor':
        successor(*sys.argv[2:])
    else:
        unittest.main()
