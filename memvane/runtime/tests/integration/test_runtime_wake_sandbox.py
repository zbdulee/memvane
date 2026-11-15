import os
from pathlib import Path
import types
from unittest.mock import patch

from tests.support.case import FixtureCase
from memvane.runtime import runtime_lease, runtime_work


class RuntimeRootGuard(FixtureCase):
    def test_without_override_home_does_not_move_per_user_root(self):
        from memvane.kernel import memvane_execution_guard
        account = self.tmp / 'account'
        account.mkdir(mode=0o700)
        (account / '.local/state/memvane/v1').mkdir(mode=0o755, parents=True)
        plugin = account / '.claude/plugins/cache/mkt/memvane/1.0.0'
        env = {key: value for key, value in os.environ.items()
               if key not in ('MEMVANE_TEST_RUNTIME_ROOT', 'QMD_SETUP_GUARD_FIXTURE', 'QMD_SANDBOX',
                              'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'HERMES_HOME')}
        env['MEMVANE_TEST_ALLOW_UNINSTALLED_ROOT'] = '1'
        with patch('pwd.getpwuid', return_value=types.SimpleNamespace(pw_dir=str(account))), \
                patch.object(memvane_execution_guard, '_plugin_root', return_value=plugin), \
                patch.dict(os.environ, env, clear=True):
            roots = []
            for home in (str(self.home), '/nonexistent-host-home'):
                with patch.dict(os.environ, {'HOME': home}):
                    roots.append(runtime_work.WakeQueue().root)
        self.assertEqual(roots, [account / '.local/state/memvane/v1/wake'] * 2)

    def test_unsandboxed_request_uses_only_fixture_runtime_root(self):
        project = self.tmp / 'project'
        project.mkdir()
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': '',
                                     'MEMVANE_TEST_RUNTIME_ROOT': str(self.runtime)}):
            self.assertEqual(runtime_work.request('laya-sync', str(project), start=False),
                             {'status': 'queued'})
            queue = runtime_work.WakeQueue()
            self.assertTrue(queue.root.is_relative_to(self.runtime))
            self.assertEqual([row['target'] for row in queue.pending()], [str(project)])

    def test_invalid_overrides_never_fall_back_to_live_root(self):
        for bad in ('relative/root', '/nonexistent-memvane-root', '/tmp', '/usr/share'):
            with self.subTest(root=bad), patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': bad}):
                with self.assertRaises(ValueError):
                    runtime_work.WakeQueue()

    def test_tmpdir_does_not_widen_platform_temp_boundary(self):
        import tempfile
        with patch.object(tempfile, 'tempdir', '/usr'), patch.dict(os.environ, {'TMPDIR': '/usr'}):
            self.assertFalse(runtime_lease.in_platform_temp('/usr/share/memvane-project'))
        self.assertTrue(runtime_lease.in_platform_temp(str(self.tmp / 'project')))

    def test_project_fixture_and_runtime_root_are_private_temporary_paths(self):
        from tests.support.project import make_project
        project = make_project(self.home, git=False)
        self.assertTrue(project.root.is_relative_to(self.home))
        self.assertTrue(self.runtime.is_relative_to(self.home))
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
