#!/usr/bin/env python3
"""Model-free regressions for the selected embedding service owner."""
import os
import fcntl
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

from resource_deferred import ResourceDeferred
from laya_index_a import IndexContractError
from laya_embed_resident import (_write_state, admit_query, ensure_running,
                                 read_owner_state, wait_for_release)


class ResidentChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        runtime = Path(self.temp.name).resolve() / 'runtime'
        runtime.mkdir(mode=0o700)
        self.manifest = {'serviceSocketPath': str(runtime / 'laya-embed.sock'),
                         'embeddingFingerprint': 'a' * 64,
                         'approvedProviderId': 'synthetic',
                         'providerArtifactSha256': 'b' * 64}

    def test_warm_service_is_reused_without_new_owner(self):
        with patch('laya_embed_resident.probe', return_value='ready'), \
             patch('laya_embed_resident.owner_lock_held', return_value=True), \
             patch('laya_resident_service.request_adopt') as spawn:
            handle = ensure_running('/synthetic/project', self.manifest)
            handle.stop()
        spawn.assert_not_called()

    def test_transient_warm_service_is_not_treated_as_resident(self):
        with patch('laya_embed_resident.probe', return_value='ready'), \
             patch('laya_embed_resident.owner_lock_held', return_value=False), \
             patch('laya_resident_service.request_adopt') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                ensure_running('/synthetic/project', self.manifest)
        spawn.assert_not_called()

    def test_cold_service_wakes_owner_and_defers_current_sync(self):
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_resident_service.request_adopt') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                ensure_running('/synthetic/project', self.manifest)
        spawn.assert_called_once()

    def test_other_identity_never_reused(self):
        with patch('laya_embed_resident.probe', return_value='mismatch'), \
             patch('laya_resident_service.request_adopt') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_identity_transition'):
                ensure_running('/synthetic/project', self.manifest)
        spawn.assert_not_called()

    def test_restore_observes_owner_socket_without_removing_it(self):
        with tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp') as root:
            path = Path(root) / 'laya-embed.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                path.chmod(0o600)
                self.assertFalse(wait_for_release(path, timeout=0))
                self.assertTrue(path.exists())
            path.unlink()
            self.assertTrue(wait_for_release(path, timeout=0))
            lock = Path(str(path) + '.resident.lock')
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(wait_for_release(path, timeout=0),
                                 'a cold-load owner has a lock before its socket')
            finally:
                os.close(fd)
            self.assertTrue(wait_for_release(path, timeout=0))

    def test_same_identity_failure_has_backoff_then_terminal_refusal(self):
        with tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp') as root:
            runtime = Path(root).resolve() / 'runtime'
            runtime.mkdir(mode=0o700)
            manifest = {**self.manifest,
                        'serviceSocketPath': str(runtime / 'laya-embed.sock')}
            _write_state(manifest, 'startup_failed', attempts=2)
            with patch('laya_embed_resident.probe', return_value='absent'), \
                 patch('laya_resident_service.request_adopt') as spawn:
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                    ensure_running('/synthetic/project', manifest)
            spawn.assert_not_called()
            _write_state(manifest, 'attestation_invalid', terminal=True, attempts=3)
            with patch('laya_embed_resident.probe', return_value='absent'), \
                 patch('laya_resident_service.request_adopt') as spawn:
                with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                    ensure_running('/synthetic/project', manifest)
            spawn.assert_not_called()

    def test_query_pressure_defers_without_stopping_loaded_model(self):
        controller = Mock()
        controller.check.return_value = {'ready': False, 'reason': 'os_memory_pressure'}
        with self.assertRaisesRegex(ResourceDeferred, 'laya_query_resource_deferred'):
            admit_query(coordinator=controller, report={'synthetic': True})
        controller.check.return_value = {'ready': True}
        self.assertIsNone(admit_query(coordinator=controller, report={'synthetic': True}))

    def test_cold_service_asks_the_user_service_once_per_call(self):
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_resident_service.request_adopt') as adopt:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                ensure_running('/synthetic/project', self.manifest)
        adopt.assert_called_once_with('/synthetic/project', root=None, spawn=None)

    def test_service_deferral_is_reported_not_raised_as_failure(self):
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_resident_service.request_adopt',
                   side_effect=ResourceDeferred('laya_service_protocol_mismatch')):
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_protocol_mismatch'):
                ensure_running('/synthetic/project', self.manifest)

    def test_failure_budget_is_terminal_even_without_an_exit_record(self):
        manifest = self.manifest
        _write_state(manifest, 'worker_exited', terminal=True, attempts=3)
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_resident_service.request_adopt') as adopt:
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                ensure_running('/synthetic/project', manifest)
        adopt.assert_not_called()
        # Even SIGKILL leaves no exit record: a surviving READY stamp at the
        # third launch must still become terminal once its socket is absent.
        _write_state(manifest, 'ready', attempts=3)
        now = read_owner_state(manifest)['at']
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_resident_service.request_adopt') as adopt, \
             patch('laya_embed_resident.time.time', return_value=now + 21):
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                ensure_running('/synthetic/project', manifest)
        self.assertTrue(read_owner_state(manifest)['terminal'])
        adopt.assert_not_called()

    def test_pending_and_selected_route_share_one_owner_identity(self):
        import laya_product_sync_worker as sync
        import laya_index_host_route as route
        from laya_embed_resident import _owned_route, _owner_identity
        with tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp') as root:
            project = Path(root).resolve() / 'project'
            project.mkdir()
            identity = {'projectRoot': str(project), 'pluginRoot': '/synthetic/plugin',
                        'installId': '1' * 32, 'journalPath': '/j', 'scopeId': '2' * 32,
                        'dbPath': '/d', 'dbIncarnation': '3' * 32,
                        'bundleManifestSha256': '4' * 64, 'ownershipManifestSha256': '5' * 64,
                        'vectorProfile': {'embeddingFingerprint': 'a' * 64}}
            proof = {'providerManifest': self.manifest}
            with patch.object(sync, '_selected', side_effect=[
                    IndexContractError('selected_route_unavailable'),
                    ({**identity, 'state': 'candidate'}, proof)]), \
                 patch.object(route, 'select_route', return_value={**identity, 'state': 'preview'}), \
                 patch.object(route, 'verify_preview_route', return_value=proof):
                pending = _owned_route(project)
                selected = _owned_route(project)
        self.assertEqual((pending[2], selected[2]), ('preview', 'selected'))
        self.assertEqual(_owner_identity(pending[0], pending[1]),
                         _owner_identity(selected[0], selected[1]))


if __name__ == '__main__':
    unittest.main()
