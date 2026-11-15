"""Proof F1: a copy outside a host install path never starts or joins the live service."""
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

from memvane.kernel import memvane_execution_guard as guard
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.resident import resident_service as svc


class LiveServiceRoot(unittest.TestCase):
    def setUp(self):
        self.account = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvl-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.account, ignore_errors=True)
        self.account.chmod(0o700)
        # The account home the guard and the service read: a temporary one.
        entry = types.SimpleNamespace(pw_dir=str(self.account))
        for patcher in (mock.patch('pwd.getpwuid', return_value=entry),
                        mock.patch.dict(os.environ, {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': ''})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.live = self.account.joinpath('.local', 'state', 'memvane', 'v1', 'service')
        self.live.mkdir(mode=0o700, parents=True)
        self.project = self.account / 'project'
        self.project.mkdir()
        self.spawned = []

    def spawn(self, argv, *, pass_fds):
        self.spawned.append(argv)

    def installed(self, *parts):
        return mock.patch.object(guard, '_plugin_root', return_value=self.account.joinpath(*parts))

    def assert_untouched(self):
        self.assertEqual(self.spawned, [])
        self.assertEqual(sorted(os.listdir(self.live)), [])

    def test_uninstalled_copy_neither_starts_nor_registers(self):
        for _ in range(2):
            with self.assertRaises(ResourceDeferred) as caught:
                svc.ensure_service(root=self.live, learning_root=self.account / 'learn',
                                   spawn=self.spawn)
            self.assertEqual(str(caught.exception), 'live_write_requires_plugin_cache')
            with self.assertRaises(ResourceDeferred) as caught:
                svc.request_adopt(str(self.project), root=self.live, spawn=self.spawn)
            self.assertEqual(str(caught.exception), 'live_write_requires_plugin_cache')
            self.assert_untouched()

    def test_uninstalled_copy_never_serves_the_live_root(self):
        fd = os.open(self.live / svc.SUCCESSOR, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        self.assertEqual(svc.main(['serve', str(self.live), '-', str(fd)]), 2)
        self.assertEqual(sorted(os.listdir(self.live)), [svc.SUCCESSOR])

    def test_host_installed_copies_start_the_service(self):
        shapes = (('.claude', 'plugins', 'cache', 'memvane-marketplace', 'memvane', '1.0.0'),
                  ('.codex', 'plugins', 'cache', 'memvane-marketplace', 'memvane', '1.0.0'),
                  ('.hermes', 'plugins', 'memvane'))
        for parts in shapes:
            self.spawned.clear()
            with self.installed(*parts):
                self.assertEqual(svc.ensure_service(root=self.live, learning_root=self.account / 'learn',
                                                    spawn=self.spawn), 'starting')
            self.assertEqual(len(self.spawned), 1, parts)
        with self.installed('.hermes', 'plugins', 'memvane-dev'):
            with self.assertRaises(ResourceDeferred):
                svc.ensure_service(root=self.live, learning_root=self.account / 'learn',
                                   spawn=self.spawn)

    def test_service_outside_live_state_is_unchanged(self):
        other = self.account / 'fixture-service'
        other.mkdir(mode=0o700)
        self.assertEqual(svc.ensure_service(root=other, learning_root=self.account / 'learn',
                                            spawn=self.spawn), 'starting')
        self.assertEqual(len(self.spawned), 1)


if __name__ == '__main__':
    unittest.main()
