"""Proof F6, verify R3: the wake queue and the sync owner lock are live writes too."""
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

from memvane.kernel import memvane_execution_guard as guard
from memvane.kernel.package import sync_owner_directory
from memvane.runtime import runtime_work


class UidSharedResources(unittest.TestCase):
    def setUp(self):
        self.account = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvw-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.account, ignore_errors=True)
        self.account.chmod(0o700)
        entry = types.SimpleNamespace(pw_dir=str(self.account))
        # No test runtime root: these resources resolve under the account's live state.
        env = {key: value for key, value in os.environ.items()
               if key not in ('MEMVANE_TEST_RUNTIME_ROOT', 'QMD_SETUP_GUARD_FIXTURE', 'QMD_SANDBOX',
                              'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'HERMES_HOME')}
        env['MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT'] = '1'  # without a runtime root it admits nothing
        for patcher in (mock.patch('pwd.getpwuid', return_value=entry),
                        mock.patch.dict(os.environ, env, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = self.account.joinpath('.local', 'state', 'memvane', 'v1')

    def refused(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), 'live_write_requires_plugin_cache')

    def test_uninstalled_copy_touches_no_live_queue_or_owner_lock(self):
        for _ in range(2):
            self.refused(runtime_work.WakeQueue)
            self.refused(lambda: sync_owner_directory(create=True))
        self.assertFalse(self.state.exists(), 'nothing created under the live state')
        self.state.joinpath('wake').mkdir(mode=0o700, parents=True)
        self.refused(lambda: runtime_work.WakeQueue(self.state / 'wake'))
        self.assertEqual(os.listdir(self.state / 'wake'), [])

    def test_installed_copy_uses_them(self):
        installed = self.account.joinpath('.claude', 'plugins', 'cache', 'mkt', 'memvane', '1.0.0')
        with mock.patch.object(guard, '_plugin_root', return_value=installed):
            self.state.mkdir(mode=0o755, parents=True)
            owner = sync_owner_directory(create=True)
            self.assertEqual(owner, self.state / 'sync-owner-locks')
            queue = runtime_work.WakeQueue()
            self.assertEqual(queue.root, self.state / 'wake')
            self.assertEqual(queue.offer('laya-sync', str(self.account / 'project'), now=1), 'queued')
            self.assertEqual(len(queue.pending()), 1)

    def test_relocated_and_symlinked_hosts_reach_the_queue(self):
        cache = ('plugins', 'cache', 'mkt', 'memvane', '1.0.0')
        codex = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvw-codex-', dir='/tmp')))
        claude = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvw-claude-', dir='/tmp')))
        for path in (codex, claude):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.account.joinpath('.claude').symlink_to(claude)
        self.state.mkdir(mode=0o755, parents=True)
        with mock.patch.object(guard, '_plugin_root', return_value=codex.joinpath(*cache)):
            self.refused(runtime_work.WakeQueue)
            with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex)}):
                queue = runtime_work.WakeQueue()
                self.assertEqual(queue.offer('laya-sync', str(self.account / 'project'), now=1), 'queued')
        with mock.patch.object(guard, '_plugin_root', return_value=claude.joinpath(*cache)):
            queue = runtime_work.WakeQueue()
            self.assertEqual(queue.root, self.state / 'wake')
            self.assertEqual(queue.offer('laya-sync', str(self.account / 'other'), now=2), 'queued')
            self.assertEqual(len(queue.pending()), 2)


if __name__ == '__main__':
    unittest.main()
