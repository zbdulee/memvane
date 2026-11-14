"""Provider socket lock failures do not touch unrelated files or start workers."""
import errno
from unittest.mock import patch

from memvane.provider.adapters import local_embed_service as local
from tests.support.case import FixtureCase


class SocketSecurity(FixtureCase):
    def test_private_lock_resource_error_preserves_os_error(self):
        manifest = {'serviceSocketPath': str(self.home / 'embed.sock')}
        lock = self.home / 'embed.sock.lock'
        lock.write_text('private lock')
        lock.chmod(0o600)
        real_open = local.os.open

        class SyntheticArtifacts:
            def __init__(self, item):
                self.manifest = item

            def _verify_artifacts(self):
                pass

        def exhausted(path, *args, **kwargs):
            if str(path) == str(lock):
                raise OSError(errno.EMFILE, 'synthetic descriptor exhaustion')
            return real_open(path, *args, **kwargs)

        with patch.object(local, 'ReviewedLocalQmdProvider', SyntheticArtifacts), \
             patch.object(local.os, 'open', side_effect=exhausted), \
             self.assertRaises(OSError) as failure:
            local.start_owned_embed_service(manifest, admit=lambda: True)
        self.assertEqual(failure.exception.errno, errno.EMFILE)
        self.assertEqual(lock.read_text(), 'private lock')

    def test_unsafe_lock_and_socket_are_rejected_before_worker_start(self):
        import socket
        from memvane.kernel.errors import IndexContractError

        class SyntheticArtifacts:
            def __init__(self, item):
                self.manifest = item

            def _verify_artifacts(self):
                pass

        for kind in ('public_lock', 'regular_socket', 'symlink_lock', 'directory_lock', 'socket_lock'):
            with self.subTest(kind=kind):
                root = self.home / kind
                root.mkdir(mode=0o700)
                path = root / 'embed.sock'
                lock = root / 'embed.sock.lock'
                manifest = {'serviceSocketPath': str(path)}
                if kind == 'public_lock':
                    lock.write_text('unsafe')
                    lock.chmod(0o644)
                elif kind == 'regular_socket':
                    path.write_text('preserve')
                elif kind == 'symlink_lock':
                    target = root / 'target'
                    target.write_text('preserve')
                    lock.symlink_to(target)
                elif kind == 'directory_lock':
                    lock.mkdir(mode=0o700)
                else:
                    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.addCleanup(listener.close)
                    listener.bind(str(lock))
                with patch.object(local, 'ReviewedLocalQmdProvider', SyntheticArtifacts), \
                     patch.object(local.subprocess, 'Popen') as spawn:
                    with self.assertRaises(IndexContractError) as caught:
                        local.start_owned_embed_service(manifest, admit=lambda: True)
                self.assertIn(str(caught.exception), ('provider_lock_invalid', 'provider_socket_invalid'))
                spawn.assert_not_called()
                if kind == 'regular_socket':
                    self.assertEqual(path.read_text(), 'preserve')
                if kind == 'symlink_lock':
                    self.assertEqual(target.read_text(), 'preserve')
