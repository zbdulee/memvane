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
             patch('laya_embed_resident.owner_lock_held', return_value=True), \
             patch('laya_embed_resident._spawn_owner') as spawn:
            handle = ensure_running('/synthetic/project', self.manifest)
            handle.stop()
        spawn.assert_not_called()

    def test_transient_warm_service_is_not_treated_as_resident(self):
        with patch('laya_embed_resident.probe', return_value='ready'), \
             patch('laya_embed_resident.owner_lock_held', return_value=False), \
             patch('laya_embed_resident._spawn_owner') as spawn:
            with self.assertRaisesRegex(ResourceDeferred, 'laya_service_preparing'):
                ensure_running('/synthetic/project', self.manifest)
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
                        'installId': '1' * 32, 'journalPath': str(runtime / 'install-journal.sqlite'),
                        'scopeId': '2' * 32, 'dbPath': str(runtime / 'index.sqlite'),
                        'dbIncarnation': '3' * 32, 'bundleManifestSha256': '4' * 64,
                        'ownershipManifestSha256': '5' * 64,
                        'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}
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

    def _contended_serve(self, refusals, *, owned_checks=None):
        """Run _serve with an activity gate refusing ``refusals`` first."""
        import laya_product_sync_worker as sync
        import laya_install_journal as journal
        import laya_local_embed_service as local
        import context_learning.resource_coordination as coordination
        import context_learning.shared as shared
        root = tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp')
        self.addCleanup(root.cleanup)
        project = Path(root.name).resolve() / 'project'
        project.mkdir()
        runtime = Path(root.name).resolve() / 'runtime'
        runtime.mkdir(mode=0o700)
        socket = runtime / 'laya-embed.sock'
        fd = os.open(runtime / 'laya-embed.sock.resident.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        manifest = {**self.manifest, 'serviceSocketPath': str(socket)}
        decision = {'projectRoot': str(project), 'pluginRoot': str(Path(__file__).resolve().parents[2]),
                    'installId': '1' * 32, 'journalPath': str(runtime / 'install-journal.sqlite'),
                    'scopeId': '2' * 32, 'dbPath': str(runtime / 'index.sqlite'),
                    'dbIncarnation': '3' * 32, 'bundleManifestSha256': '4' * 64,
                    'ownershipManifestSha256': '5' * 64,
                    'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}
        proof = {'providerManifest': manifest}
        _write_state(manifest, 'starting', attempts=1)
        service = Mock()
        service.process.poll.return_value = None
        pending = list(refusals)
        entered = []

        class Controller:
            @contextmanager
            def activity(self, *_args):
                if pending:
                    raise ResourceDeferred(pending.pop(0))
                entered.append(True)
                yield

            def check(self, *_args):
                return {'ready': True}
        if owned_checks is None:
            # each retry's still_owned, then after-READY; the idle loop withdraws
            owned_checks = len(refusals) + 1
        routes = [(decision, proof)] * (1 + owned_checks) + [IndexError('withdrawn')]
        sleeps = []
        result = {}
        with patch.object(sync, '_selected', side_effect=routes), \
             patch.object(journal, 'verify_worker_attestation', return_value=manifest), \
             patch.object(local, 'start_owned_embed_service', return_value=service) as start, \
             patch.object(coordination, 'Coordinator', Controller), \
             patch.object(shared, 'resource_report', return_value={}), \
             patch('laya_embed_resident.time.sleep', side_effect=sleeps.append):
            try:
                result['status'] = _serve(str(project), str(socket),
                                          manifest['embeddingFingerprint'], fd)
            except Exception as exc:
                result['error'] = exc
        result.update(start=start, service=service, sleeps=sleeps, entered=entered,
                      manifest=manifest)
        return result

    def test_lane_contention_keeps_lease_and_retries_until_ready(self):
        import laya_embed_resident as resident
        seen = []
        real_write = resident._write_state
        with patch('laya_embed_resident._write_state',
                   side_effect=lambda m, reason, **kw: (seen.append(reason),
                                                        real_write(m, reason, **kw))):
            run = self._contended_serve(['learning_compute_busy', 'learning_deferred_inference',
                                         'learning_deferred_resource'])
        self.assertEqual(run.get('status'), 0, run.get('error'))
        run['start'].assert_called_once()
        self.assertEqual(run['entered'], [True])
        self.assertEqual(run['sleeps'][:3], [resident.LANE_RETRY_SECONDS] * 3)
        # It became ready instead of exiting 75 into the wake backoff.
        self.assertEqual(seen, ['ready', 'starting'])

    def test_lane_contention_gives_up_after_the_bounded_window(self):
        import laya_embed_resident as resident
        refusals = ['learning_compute_busy'] * resident.LANE_RETRY_LIMIT
        run = self._contended_serve(refusals)
        self.assertIsInstance(run.get('error'), ResourceDeferred)
        self.assertEqual(str(run['error']), 'learning_compute_busy')
        self.assertEqual(run['sleeps'], [resident.LANE_RETRY_SECONDS] * (resident.LANE_RETRY_LIMIT - 1))
        run['start'].assert_not_called()

    def test_other_deferrals_and_withdrawal_during_the_wait_do_not_retry(self):
        run = self._contended_serve(['qmd_startup_recovery_required'])
        self.assertEqual(str(run.get('error')), 'qmd_startup_recovery_required')
        self.assertEqual(run['sleeps'], [])
        run['start'].assert_not_called()
        # The route moves on while the owner waits: a normal withdrawal.
        moved = self._contended_serve(['learning_compute_busy'] * 3, owned_checks=1)
        self.assertEqual(moved.get('status'), 0, moved.get('error'))
        moved['start'].assert_not_called()
        row = read_owner_state(moved['manifest'])
        self.assertEqual((row['reason'], row['attempts']), ('starting', 0))

    def test_pending_owner_survives_selection_of_the_same_installation(self):
        import laya_product_sync_worker as sync
        import laya_index_host_route as route
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
            identity = {'projectRoot': str(project),
                        'pluginRoot': str(Path(__file__).resolve().parents[2]),
                        'installId': '1' * 32,
                        'journalPath': str(runtime / 'install-journal.sqlite'),
                        'scopeId': '2' * 32, 'dbPath': str(runtime / 'index.sqlite'),
                        'dbIncarnation': '3' * 32, 'bundleManifestSha256': '4' * 64,
                        'ownershipManifestSha256': '5' * 64,
                        'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}
            preview = {**identity, 'state': 'preview'}
            selected = {**identity, 'state': 'candidate'}
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
                with patch.object(sync, '_selected', side_effect=[
                        IndexContractError('selected_route_unavailable'),
                        IndexContractError('selected_route_unavailable'),
                        (selected, proof),
                        IndexContractError('selected_route_unavailable')]) as selected_check, \
                     patch.object(route, 'select_route', side_effect=[
                         preview, preview, {'state': 'blocked'}]) as route_select, \
                     patch.object(route, 'verify_preview_route', return_value=proof), \
                     patch.object(local, 'start_owned_embed_service', return_value=service) as start, \
                     patch.object(coordination, 'Coordinator', Controller), \
                     patch.object(shared, 'resource_report', return_value={}), \
                     patch('laya_embed_resident.time.sleep'):
                    self.assertEqual(_serve(str(project), str(socket),
                                            manifest['embeddingFingerprint'], fd), 0)
                start.assert_called_once()
                service.stop.assert_called_once()
                self.assertEqual(selected_check.call_count, 4)
                self.assertEqual(route_select.call_count, 3)
                self.assertEqual(read_owner_state(manifest)['attempts'], 0)
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
