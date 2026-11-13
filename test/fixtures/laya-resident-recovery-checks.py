"""Synthetic retry-clock checks; no model, runtime queue or live state access."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_embed_resident as owner
import laya_resident_service as svc
from laya_index_a import IndexContractError
from memvane.kernel.resource_deferred import ResourceDeferred


class RecoveryChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='lr-recover-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.base.chmod(0o700)
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

    def test_security_terminal_never_expires(self):
        for reason in ('attestation_invalid', 'identity_invalid'):
            with self.subTest(reason=reason):
                self.stamp(reason)
                probe, clock = self.cold(100000)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt:
                    with self.assertRaisesRegex(IndexContractError, 'provider_start_failed'):
                        owner.ensure_running('/synthetic/project', self.manifest)
                adopt.assert_not_called()

    def test_real_worker_path_security_failures_are_terminal(self):
        from memvane.provider.adapters import local_embed_service as local
        class SyntheticArtifacts:
            def __init__(self, manifest):
                self.manifest = manifest
            def _verify_artifacts(self):
                pass  # no model or real artifact files in this fixture
        for kind in ('public_lock', 'regular_socket', 'symlink_lock',
                     'directory_lock', 'socket_lock'):
            with self.subTest(kind=kind):
                base = self.base / kind
                base.mkdir(mode=0o700)
                root = base / 'service'
                root.mkdir(mode=0o700)
                project = str(base / 'project')
                Path(project).mkdir(mode=0o700)
                manifest = {**self.manifest, 'serviceSocketPath': str(base / 'install.sock')}
                proof = {'providerManifest': manifest}
                decision = {'projectRoot': project}
                service = svc.Service(root, bundle={'version': '1.0.0', 'digest': 'a'},
                                      prove=lambda _p: (decision, proof, 'selected'),
                                      attest=lambda *_a: None, admit=lambda: True,
                                      retire=lambda *_a: None)
                svc.register(project, root=root)
                key = svc._worker_key(manifest)
                worker = svc.Worker(key, str(base / 'worker.sock'))
                service.workers[key] = worker
                lock = Path(worker.socket_path + '.lock')
                if kind == 'public_lock':
                    lock.write_text('unsafe lock')
                    lock.chmod(0o644)
                elif kind == 'regular_socket':
                    Path(worker.socket_path).write_text('not a socket')
                elif kind == 'symlink_lock':
                    target = base / 'preserved'
                    target.write_text('must not be opened through a link')
                    lock.symlink_to(target)
                elif kind == 'directory_lock':
                    lock.mkdir(mode=0o700)
                else:
                    import socket
                    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.addCleanup(listener.close)
                    listener.bind(str(lock))
                with patch.object(local, 'ReviewedLocalQmdProvider', SyntheticArtifacts), \
                     patch.object(local.subprocess, 'Popen',
                                  side_effect=AssertionError('no model may start')) as spawn:
                    for now in (1000, 1011, 1032):
                        with patch.object(svc.time, 'monotonic', return_value=now), \
                             patch.object(owner.time, 'time', return_value=now):
                            service._load_attested(project, decision, proof, 'selected',
                                                   manifest, worker, 'seal')
                    row = owner.read_owner_state(manifest)
                    self.assertTrue(row['terminal'], row)
                    self.assertEqual((row['reason'], worker.state),
                                     ('identity_invalid', 'terminal'))
                    probe, clock = self.cold(2000)
                    with probe, clock, self.assertRaisesRegex(IndexContractError,
                                                              'provider_start_failed'):
                        owner.ensure_running(project, manifest)
                spawn.assert_not_called()
                if kind == 'regular_socket':
                    self.assertEqual(Path(worker.socket_path).read_text(), 'not a socket')
                elif kind == 'symlink_lock':
                    self.assertEqual(target.read_text(), 'must not be opened through a link')
                elif kind == 'directory_lock':
                    self.assertTrue(lock.is_dir())

    def test_recall_preserves_lexical_during_cooldown_and_wakes_after_it(self):
        from memvane.host import host_response as host
        from memvane.journal import install_journal as journal
        self.stamp('startup_failed')
        proof = {'providerManifest': self.manifest, 'selectionEpoch': 1}
        decision = {'projectRoot': '/synthetic/project',
                    'pluginRoot': str(Path(host.__file__).resolve().parents[1])}
        for now, expected_calls in ((1299, 0), (1301, 1)):
            with self.subTest(now=now):
                probe, clock = self.cold(now)
                with probe, clock, patch.object(svc, 'request_adopt') as adopt, \
                     patch.object(journal, 'verify_selected_route', return_value=proof), \
                     patch.object(host, '_recall_epoch'), \
                     patch.object(host, '_recall_sync_wake', return_value={'status': 'queued'}):
                    self.assertEqual(host._wake_provider(decision, proof),
                                     'selected_provider_retry_wait')
                self.assertEqual(adopt.call_count, expected_calls)

    def test_private_lock_resource_error_keeps_its_transient_os_code(self):
        import errno
        from memvane.provider.adapters import local_embed_service as local
        class SyntheticArtifacts:
            def __init__(self, manifest):
                self.manifest = manifest
            def _verify_artifacts(self):
                pass
        lock = Path(self.manifest['serviceSocketPath'] + '.lock')
        lock.write_text('valid private lock')
        lock.chmod(0o600)
        real_open = local.os.open
        def exhausted(path, *args, **kwargs):
            if str(path) == str(lock):
                raise OSError(errno.EMFILE, 'synthetic descriptor exhaustion')
            return real_open(path, *args, **kwargs)
        with patch.object(local, 'ReviewedLocalQmdProvider', SyntheticArtifacts), \
             patch.object(local.os, 'open', side_effect=exhausted), \
             self.assertRaises(OSError) as failure:
            local.start_owned_embed_service(self.manifest, admit=lambda: True)
        self.assertEqual(failure.exception.errno, errno.EMFILE)
        self.assertEqual(lock.read_text(), 'valid private lock')

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
                              retire=lambda *_a: None)
        key = svc._worker_key(self.manifest)
        worker = svc.Worker(key, str(self.base / 'worker.sock'))
        service.workers[key] = worker
        for now in (1000, 1011, 1032):
            with patch.object(svc.time, 'monotonic', return_value=now), \
                 patch.object(owner.time, 'time', return_value=now):
                service._load_attested(project, decision, proof, 'selected',
                                       self.manifest, worker, 'seal')
        class InlineThread:
            def __init__(self, *, target, args, **_kw):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        with patch.object(svc.threading, 'Thread', InlineThread), \
             patch.object(svc.time, 'monotonic', return_value=1333), \
             patch.object(owner.time, 'time', return_value=1333):
            service._load(project, decision, proof, 'selected', self.manifest, key, 'seal')
        self.assertEqual((len(calls), len(attests), worker.state), (4, 4, 'ready'))


if __name__ == '__main__':
    unittest.main()
