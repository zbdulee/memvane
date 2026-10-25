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
from laya_embed_resident import (_serve, _spawn_owner, _write_state, admit_query, ensure_running,
                                 main, read_owner_state, wait_for_release)


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
             patch('laya_embed_resident._spawn_owner') as spawn:
            handle = ensure_running('/synthetic/project', self.manifest)
            handle.stop()
        spawn.assert_not_called()

    def test_cold_service_wakes_owner_and_defers_current_sync(self):
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_embed_resident._spawn_owner') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                ensure_running('/synthetic/project', self.manifest)
        spawn.assert_called_once()

    def test_other_identity_never_reused(self):
        with patch('laya_embed_resident.probe', return_value='mismatch'), \
             patch('laya_embed_resident._spawn_owner') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_identity_transition'):
                ensure_running('/synthetic/project', self.manifest)
        spawn.assert_not_called()

    def test_detached_owner_keeps_model_until_selection_is_lost(self):
        import laya_product_sync_worker as sync
        import laya_install_journal as journal
        import laya_local_embed_service as local
        import context_learning.resource_coordination as coordination
        import context_learning.shared as shared
        with tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp') as root:
            project = Path(root).resolve() / 'project'
            project.mkdir()
            runtime = Path(root).resolve() / 'runtime'
            runtime.mkdir(mode=0o700)
            socket = runtime / 'laya-embed.sock'
            lock = runtime / 'laya-embed.sock.resident.lock'
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            manifest = {**self.manifest, 'serviceSocketPath': str(socket)}
            decision = {'projectRoot': str(project), 'pluginRoot': str(Path(__file__).resolve().parents[2]),
                        'installId': '1' * 32, 'journalPath': str(runtime / 'install-journal.sqlite')}
            proof = {'providerManifest': manifest}
            service = Mock()
            service.process.poll.return_value = None
            class Controller:
                @contextmanager
                def activity(self, *_args):
                    yield
                def check(self, *_args):
                    return {'ready': True}
            try:
                with patch.object(sync, '_selected', side_effect=[(decision, proof),
                          (decision, proof), IndexError('route restored')]), \
                     patch.object(journal, 'verify_worker_attestation', return_value=manifest), \
                     patch.object(local, 'start_owned_embed_service', return_value=service) as start, \
                     patch.object(coordination, 'Coordinator', Controller), \
                     patch.object(shared, 'resource_report', return_value={}), \
                     patch('laya_embed_resident.time.sleep'):
                    self.assertEqual(_serve(str(project), str(socket),
                                            manifest['embeddingFingerprint'], fd), 0)
                start.assert_called_once()
                service.stop.assert_called_once()
            finally:
                os.close(fd)

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
                 patch('laya_embed_resident._spawn_owner') as spawn:
                with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                    ensure_running('/synthetic/project', manifest)
            spawn.assert_not_called()
            _write_state(manifest, 'attestation_invalid', terminal=True, attempts=3)
            with patch('laya_embed_resident.probe', return_value='absent'), \
                 patch('laya_embed_resident._spawn_owner') as spawn:
                with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                    ensure_running('/synthetic/project', manifest)
            spawn.assert_not_called()

    def test_owner_exit_records_fixed_failure_and_resource_wait(self):
        with tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp') as root:
            runtime = Path(root).resolve() / 'runtime'
            runtime.mkdir(mode=0o700)
            manifest = {**self.manifest,
                        'serviceSocketPath': str(runtime / 'laya-embed.sock')}
            args = ['serve', '/synthetic/project', manifest['serviceSocketPath'],
                    manifest['embeddingFingerprint'], '3']
            with patch('laya_embed_resident._serve',
                       side_effect=ResourceDeferred('learning_deferred_resource')):
                self.assertEqual(main(args), 75)
            state = read_owner_state(manifest)
            self.assertEqual((state['reason'], state['attempts'], state['terminal']),
                             ('resource_deferred', 0, False))
            with patch('laya_embed_resident._serve',
                       side_effect=IndexContractError('provider_attestation_invalid')):
                self.assertEqual(main(args), 1)
            state = read_owner_state(manifest)
            self.assertEqual((state['reason'], state['terminal']),
                             ('attestation_invalid', True))
            self.assertEqual((runtime / 'laya-embed.sock.resident.state').stat().st_mode & 0o777,
                             0o600)

    def test_query_pressure_defers_without_stopping_loaded_model(self):
        controller = Mock()
        controller.check.return_value = {'ready': False, 'reason': 'os_memory_pressure'}
        with self.assertRaisesRegex(ResourceDeferred, 'laya_query_resource_deferred'):
            admit_query(coordinator=controller, report={'synthetic': True})
        controller.check.return_value = {'ready': True}
        self.assertIsNone(admit_query(coordinator=controller, report={'synthetic': True}))

    def test_repeated_worker_crash_stops_same_identity_respawns(self):
        manifest = self.manifest
        args = ['serve', '/synthetic/project', manifest['serviceSocketPath'],
                manifest['embeddingFingerprint'], '3']
        with patch('laya_embed_resident._serve',
                   side_effect=IndexContractError('provider_worker_exited')):
            for attempt in range(1, 4):
                self.assertEqual(main(args), 1)
                self.assertEqual(read_owner_state(manifest)['attempts'], attempt)
        self.assertTrue(read_owner_state(manifest)['terminal'])
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_embed_resident._spawn_owner') as spawn:
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                ensure_running('/synthetic/project', manifest)
        spawn.assert_not_called()
        # Even SIGKILL leaves no exit record: a surviving READY stamp at the
        # third launch must still become terminal once its socket is absent.
        _write_state(manifest, 'ready', attempts=3)
        now = read_owner_state(manifest)['at']
        with patch('laya_embed_resident.probe', return_value='absent'), \
             patch('laya_embed_resident._spawn_owner') as spawn, \
             patch('laya_embed_resident.time.time', return_value=now + 21):
            with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                ensure_running('/synthetic/project', manifest)
        self.assertTrue(read_owner_state(manifest)['terminal'])
        spawn.assert_not_called()

    def test_single_spawn_records_start_and_passes_private_owner_lease(self):
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': ''}), \
             patch('laya_embed_resident.subprocess.Popen') as spawned:
            _spawn_owner('/synthetic/project', self.manifest)
            row = read_owner_state(self.manifest)
            self.assertEqual((row['reason'], row['attempts']), ('starting', 1))
            self.assertTrue(spawned.call_args.kwargs['start_new_session'])
            self.assertEqual(len(spawned.call_args.kwargs['pass_fds']), 1)
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_backoff'):
                _spawn_owner('/synthetic/project', self.manifest)
        spawned.assert_called_once()


if __name__ == '__main__':
    unittest.main()
