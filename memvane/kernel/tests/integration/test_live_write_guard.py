"""Proof F6 and F9, verify R3: the live-write path-shape check and its test-only bypass."""
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

from memvane.kernel import memvane_execution_guard as guard

RELOCATIONS = ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'HERMES_HOME')


class LiveWriteGuard(unittest.TestCase):
    def setUp(self):
        self.account = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvg-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.account, ignore_errors=True)
        entry = types.SimpleNamespace(pw_dir=str(self.account))
        for patcher in (mock.patch('pwd.getpwuid', return_value=entry),
                        mock.patch.dict(os.environ, {'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '',
                                                     'HOME': str(self.account)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for variable in RELOCATIONS:
            os.environ.pop(variable, None)  # restored by the patch.dict above
        self.live = self.account.joinpath('.local', 'state', 'memvane', 'v1')

    def elsewhere(self):
        path = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvg-host-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def admitted(self, root):
        with mock.patch.object(guard, '_plugin_root', return_value=Path(os.path.realpath(root))):
            guard.require_cached_plugin_for_live_write(self.live / 'wake')

    def refused(self, state=None):
        with self.assertRaises(ValueError) as caught:
            guard.require_cached_plugin_for_live_write(state)
        self.assertEqual(str(caught.exception), 'live_write_requires_plugin_cache')

    def test_host_install_shapes(self):
        allowed = (('.claude', 'plugins', 'cache', 'mkt', 'memvane', '1.0.0'),
                   ('.codex', 'plugins', 'cache', 'mkt', 'memvane', '1.2.3'),
                   ('.hermes', 'plugins', 'memvane'))
        refused = (('.claude', 'plugins', 'cache', 'mkt', 'other', '1.0.0'),
                   ('.claude', 'plugins', 'cache', 'mkt', 'memvane'),
                   ('.hermes', 'plugins', 'memvane', 'nested'),
                   ('.hermes', 'plugins', 'memvane-dev'),
                   ('.hermes', 'memvane'),
                   ('work', 'memvane-wt', 'v1'))
        for parts in allowed:
            self.assertTrue(guard.installed_plugin_root(self.account.joinpath(*parts), self.account),
                            parts)
            with mock.patch.object(guard, '_plugin_root', return_value=self.account.joinpath(*parts)):
                guard.require_cached_plugin_for_live_write(self.live / 'installs' / ('a' * 32))
        for parts in refused:
            self.assertFalse(guard.installed_plugin_root(self.account.joinpath(*parts), self.account),
                             parts)
            with mock.patch.object(guard, '_plugin_root', return_value=self.account.joinpath(*parts)):
                self.refused(self.live / 'wake')
        # This checkout is no host install: live state refuses, anything else passes.
        self.refused()
        self.refused(self.live / 'sync-owner-locks')
        guard.require_cached_plugin_for_live_write(self.account / 'elsewhere')

    def test_symlinked_and_relocated_host_roots(self):
        cache = ('plugins', 'cache', 'mkt', 'memvane', '1.0.0')
        # ~/.claude is a symlink: the copy runs from the link's real target.
        claude = self.elsewhere()
        self.account.joinpath('.claude').symlink_to(claude)
        self.admitted(claude.joinpath(*cache))
        self.refused_from(claude.joinpath('plugins', 'cache', 'mkt', 'other', '1.0.0'))
        # CODEX_HOME relocates Codex; CLAUDE_CONFIG_DIR is given through a symlink.
        codex, config = self.elsewhere(), self.elsewhere()
        link = self.elsewhere() / 'config-link'
        link.symlink_to(config)
        hermes = self.elsewhere()
        cases = ((codex.joinpath(*cache), codex.joinpath('plugins', 'cache', 'mkt', 'memvane')),
                 (config.joinpath(*cache), config / 'memvane'),
                 (hermes.joinpath('plugins', 'memvane'), hermes.joinpath('plugins', 'memvane', 'x')))
        for (allowed, refused), env in zip(cases, ({'CODEX_HOME': str(codex)},
                                                    {'CLAUDE_CONFIG_DIR': str(link)},
                                                    {'HERMES_HOME': str(hermes)})):
            self.refused_from(allowed)
            with mock.patch.dict(os.environ, env):
                self.admitted(allowed)
                self.refused_from(refused)
                self.refused_from(Path(os.path.realpath(env[next(iter(env))])))
        # A relative relocation names nothing.
        with mock.patch.dict(os.environ, {'CODEX_HOME': 'relative'}):
            self.refused_from(Path(os.path.realpath('relative')).joinpath(*cache))
        # HOME as a symlink to a home that is not the user database's.
        other = self.elsewhere()
        home_link = self.elsewhere() / 'home-link'
        home_link.symlink_to(other)
        installed = other.joinpath('.codex', *cache)
        self.refused_from(installed)
        with mock.patch.dict(os.environ, {'HOME': str(home_link)}):
            self.admitted(installed)
            self.refused_from(other / 'work' / 'memvane')
        # A development checkout stays refused whatever the hosts' roots are.
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex), 'HERMES_HOME': str(hermes)}):
            self.refused()

    def refused_from(self, root):
        with mock.patch.object(guard, '_plugin_root', return_value=Path(os.path.realpath(root))):
            self.refused(self.live / 'wake')

    def test_bypass_needs_a_test_runtime_root(self):
        temp_root = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvg-rt-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, temp_root, ignore_errors=True)
        cases = (({'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1'}, False),
                 ({'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1', 'MEMVANE_TEST_RUNTIME_ROOT': ''}, False),
                 ({'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1', 'MEMVANE_TEST_RUNTIME_ROOT': 'relative'}, False),
                 ({'MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT': '1', 'MEMVANE_TEST_RUNTIME_ROOT': str(temp_root)}, True),
                 ({'MEMVANE_TEST_RUNTIME_ROOT': str(temp_root)}, False))
        for env, admitted in cases:
            with mock.patch.dict(os.environ, env, clear=False):
                if 'MEMVANE_TEST_RUNTIME_ROOT' not in env:
                    os.environ.pop('MEMVANE_TEST_RUNTIME_ROOT', None)
                if admitted:
                    guard.require_cached_plugin_for_live_write(self.live)
                else:
                    self.refused(self.live)


if __name__ == '__main__':
    unittest.main()
