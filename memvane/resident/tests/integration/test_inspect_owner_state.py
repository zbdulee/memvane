"""``inspect_owner_state``: the records the resident's own writers leave, read without a write or a lock."""
from __future__ import annotations

import os
from pathlib import Path
import socket
from unittest import mock

from tests.support.case import FixtureCase
from memvane.kernel import bundle_paths
from memvane.provider import api as provider
from memvane.resident import api
from memvane.resident import embed_resident as owner, resident_service as svc
from memvane.runtime.api import service_root


def tree(*roots):
    """Every path under ``roots`` with its bytes and mtime."""
    found = {}
    for root in roots:
        for path in sorted(Path(root).rglob('*')):
            info = path.lstat()
            found[str(path)] = (path.read_bytes() if path.is_file() else None, info.st_mtime_ns)
    return found


class InspectOwnerState(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = self.home / 'p'
        self.project.mkdir(mode=0o700)
        worker = bundle_paths.bundle_file(bundle_paths.WORKER_RELATIVE_PATH)
        artifact = bundle_paths.bundle_file(bundle_paths.PROVIDER_RELATIVE_PATH)
        self.manifest = {'approvedProviderId': 'synthetic', 'embeddingFingerprint': 'a' * 64,
                         'workerPath': str(worker), 'workerSha256': provider.hash_file(str(worker)),
                         'providerPath': str(artifact),
                         'providerArtifactSha256': provider.hash_file(str(artifact)),
                         'dimension': 4, 'modelId': 'fixture', 'modelSha256': 'c' * 64,
                         'serviceSocketPath': str(self.project / 'e.sock')}

    def inspect(self):
        before = tree(self.project, self.runtime)
        view = api.inspect_owner_state(self.manifest)
        self.assertEqual(tree(self.project, self.runtime), before)
        self.assertFalse(Path(self.manifest['serviceSocketPath'] + '.resident.lock').exists())
        return view

    def test_nothing_recorded_reads_absent_and_creates_nothing(self):
        view = self.inspect()
        self.assertEqual(view, {'socket': {'status': 'absent'}, 'installation': {'status': 'absent'},
                                'sharedWorker': {'status': 'absent'}})
        self.assertFalse((self.runtime / 'laya-inference').exists())

    def test_an_invalid_attestation_waits_an_hour_for_its_next_proof(self):
        owner._write_state(self.manifest, 'attestation_invalid', terminal=True, attempts=owner._MAX_FAILURES)
        installation = self.inspect()['installation']
        self.assertEqual((installation['reason'], installation['terminal'], installation['exhausted']),
                         ('attestation_invalid', True, True))
        self.assertTrue(owner._ATTESTATION_RETRY_SECONDS - 60 < installation['retryInSeconds']
                        <= owner._ATTESTATION_RETRY_SECONDS)
        later = installation['at'] + owner._ATTESTATION_RETRY_SECONDS + 1
        with mock.patch.object(owner.time, 'time', return_value=later):
            installation = self.inspect()['installation']
        self.assertEqual((installation['exhausted'], installation['retryInSeconds'], installation['retryNever']),
                         (True, None, False))

    def test_a_terminal_identity_failure_never_retries_on_its_own(self):
        owner._write_state(self.manifest, 'identity_invalid', terminal=True)
        installation = self.inspect()['installation']
        self.assertEqual((installation['retryInSeconds'], installation['retryNever']), (None, True))

    def test_an_exhausted_start_is_shown_where_the_failure_decision_would_write(self):
        owner._write_state(self.manifest, 'starting', attempts=owner._MAX_FAILURES)
        with mock.patch.object(owner.time, 'time', return_value=os.path.getmtime(
                self.manifest['serviceSocketPath'] + '.resident.state') + 3600):
            installation = self.inspect()['installation']
        self.assertEqual((installation['reason'], installation['attempts'], installation['exhausted']),
                         ('starting', owner._MAX_FAILURES, False))
        # The writing reader records ``worker_exited`` here; the inspection above did not.
        with mock.patch.object(owner.time, 'time', return_value=installation['at'] + 3600), \
                self.assertRaises(owner.ResourceDeferred):
            api.failure_decision(self.manifest)
        self.assertEqual(owner.read_owner_state(self.manifest)['reason'], 'worker_exited')

    def test_a_projected_shared_launch_failure_keeps_the_local_proof_count(self):
        owner._record_shared_wait(self.manifest, 'startup_failed', False, owner._MAX_FAILURES)
        installation = self.inspect()['installation']
        self.assertEqual((installation['reason'], installation['attempts'], installation['proofAttempts'],
                          installation['effectiveAttempts'], installation['exhausted']),
                         ('startup_failed', owner._MAX_FAILURES, 0, 0, False))

    def test_the_shared_worker_record_carries_its_retry_delay(self):
        root = service_root(create=True)
        key = svc._worker_key(self.manifest)
        worker = svc.Worker(key, svc._worker_socket(root, key))
        owner._record_process_outcome(worker, self.manifest, 'startup_failed')
        shared = self.inspect()['sharedWorker']
        self.assertEqual((shared['status'], shared['socket'], shared['reason'], shared['attempts']),
                         ('present', os.path.basename(worker.socket_path), 'startup_failed', 1))
        self.assertTrue(0 < shared['retryInSeconds'] <= owner._process_delay('startup_failed', 1))
        # The installation's own record is a separate file: still absent.
        self.assertEqual(self.inspect()['installation'], {'status': 'absent'})

    def test_a_socket_path_that_is_not_a_socket_is_named(self):
        Path(self.manifest['serviceSocketPath']).write_text('')
        os.chmod(self.manifest['serviceSocketPath'], 0o600)
        self.assertEqual(self.inspect()['socket'], {'status': 'error', 'error': 'provider_socket_invalid'})

    def test_a_listening_socket_answers_through_probe(self):
        path = self.manifest['serviceSocketPath']
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            os.chmod(path, 0o600)
            # Not listening: a refused connection reads as absent, like ``probe``.
            self.assertEqual(self.inspect()['socket'], {'status': 'absent'})

    def test_a_damaged_record_is_named_and_left_in_place(self):
        path = Path(self.manifest['serviceSocketPath'] + '.resident.state')
        path.write_text('{}')
        path.chmod(0o600)
        self.assertEqual(self.inspect()['installation'],
                         {'status': 'unreadable', 'error': 'provider_owner_state_invalid'})
        self.assertEqual(path.read_text(), '{}')
