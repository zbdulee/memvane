"""Model-free owned resident process and installation harness."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from memvane.kernel.errors import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.provider import api as provider_api
from memvane.resident import embed_resident as resident
from memvane.resident import resident_service as svc
from tests.support.case import FixtureCase

DIM = 4
TIME_SCALE = .02  # subprocess drain checks: budget 9.6 s, successor wait 12 s
RUNTIME = 'qmd-2.5.3-node-llama-cpp-3.18.1'
RECIPE = 'qmd-query-format-single-document-v1'


def manifest_for(socket_path, *, model='1', dimension=DIM):
    digests = {'model': model * 64, 'provider': '2' * 64, 'worker': '3' * 64,
               'node': '4' * 64, 'entry': '5' * 64, 'closure': '6' * 64}
    fingerprint = provider_api.recipe_fingerprint(
        model_id=provider_api.default_embed_model_uri(), model_sha256=digests['model'], runtime_id=RUNTIME,
        recipe_id=RECIPE, provider_sha256=digests['provider'], worker_sha256=digests['worker'],
        node_sha256=digests['node'], module_entry_sha256=digests['entry'],
        closure_sha256=digests['closure'])
    return {'approvedProviderId': 'local-qmd-synthetic', 'providerArtifactSha256': digests['provider'],
            'providerRelativePath': 'memvane/provider/adapters/local_embed_provider.py',
            'providerPath': '/synthetic/memvane/provider/adapters/local_embed_provider.py',
            'workerPath': '/synthetic/memvane/provider/adapters/laya_local_embed_worker.mjs', 'workerSha256': digests['worker'],
            'serviceSocketPath': str(socket_path), 'nodePath': '/synthetic/node',
            'nodeSha256': digests['node'], 'moduleEntryPath': '/synthetic/entry.mjs',
            'moduleEntrySha256': digests['entry'], 'modelPath': '/synthetic/model.gguf',
            'modelSha256': digests['model'], 'runtimeId': RUNTIME, 'modelId': provider_api.default_embed_model_uri(),
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


class Fixture(FixtureCase):
    def setUp(self):
        super().setUp()
        self.base = self.home
        self.root = self.base / 'svc'
        self.root.mkdir(mode=0o700)
        self.loads = []
        self.handles = []
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
        for handle in self.handles:
            if handle.process.poll() is None:
                handle.stop()

    def project(self, name, **variant):
        project = self.base / name
        project.mkdir()
        runtime = self.base / (name + '-state') / 'runtime'
        runtime.mkdir(parents=True, mode=0o700)
        runtime.parent.chmod(0o700)
        manifest = manifest_for(runtime / 'laya-embed.sock', **variant)
        decision = {'projectRoot': str(project), 'pluginRoot': '/synthetic/plugin',
                    'installId': name, 'journalPath': str(runtime / 'journal'),
                    'scopeId': name, 'storePath': str(runtime / 'db'), 'storeIncarnation': name,
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
            [sys.executable, '-B', '-m', 'memvane.resident.tests.support', '--fake-worker',
             manifest['serviceSocketPath'],
             json.dumps({key: manifest[key] for key in resident._IDENTITY}),
             str(delay), str(record), str(self.base / 'slow'), str(manifest['dimension'])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        (self.base / ('worker-%s.pid' % process.pid)).write_text(str(process.pid))
        handle = Handle(process, manifest['serviceSocketPath'])
        self.handles.append(handle)
        if process.stdout.readline() != b'READY\n':
            raise IndexContractError('provider_start_failed')
        self.loads.append(process.pid)
        return handle

    def service(self, *, bundle=None, start=True):
        service = svc.Service(self.root, bundle=bundle or {'version': '1.0.0', 'digest': 'a'},
                              prove=self.prove, attest=lambda *_a: None,
                              start_worker=self.start_worker,
                              admit=lambda: self.admit_value,
                              tick=.05)
        # Every tick re-proves ownership here; the throttle has its own check.
        service.owner_recheck_seconds = 0
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
    provider = provider_api.reviewed_provider(manifest)
    if kind == 'document':
        return provider.embed_document(text)
    keys = {'profileId': 'profile_id', 'modelId': 'model_id',
            'embeddingFingerprint': 'embedding_fingerprint', 'dimension': 'dimension',
            'metric': 'metric'}
    return provider(text, {public: provider.profile[internal]
                           for public, internal in keys.items()})['vector']


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


if __name__ == '__main__' and len(sys.argv) == 8 and sys.argv[1] == '--fake-worker':
    fake_worker(*sys.argv[2:])
