"""Guard: a test's runtime wakes never reach the user's runtime root.

The per-user runtime root (wake queue, worker lease, coordinator, monitor)
lives under the passwd home, whatever HOME a host sets. A test run sets
``MEMVANE_TEST_RUNTIME_ROOT`` (``npm test`` through the development guard, the
shared Laya fixtures in their temp HOME), and every participant resolves that
root instead, so a test that clears the sandbox flags still cannot write the
user's queue. The passwd home is a decoy directory here; nothing outside this
test's temp tree is opened.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
CORE = FIXTURES.parents[1] / 'core'
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(FIXTURES))

import runtime_work  # noqa: E402

QUEUE = 'runtime-wakes.sqlite3'
REAL_POPEN = subprocess.Popen


def queues(root):
    return sorted(str(path) for path in Path(root).rglob(QUEUE))


class Decoy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='memvane-wake-sandbox-')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.passwd_home, self.home = base / 'passwd-home', base / 'home'
        for path in (self.passwd_home, self.home):
            path.mkdir(mode=0o700)
        real = pwd.getpwuid
        decoy = str(self.passwd_home)

        def getpwuid(uid):
            entry = real(uid)
            if uid != os.getuid():
                return entry
            return pwd.struct_passwd((entry.pw_name, entry.pw_passwd, entry.pw_uid,
                                      entry.pw_gid, entry.pw_gecos, decoy, entry.pw_shell))
        for patcher in (mock.patch.object(pwd, 'getpwuid', getpwuid),
                        mock.patch.dict(os.environ, {
                            'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '',
                            'QMD_SANDBOX': '', 'QMD_RUNTIME_DISPATCH': '1',
                            'MEMVANE_TEST_RUNTIME_ROOT': ''}),
                        mock.patch.object(runtime_work.subprocess, 'Popen',
                                          side_effect=AssertionError('runtime worker spawned'))):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestRuntimeRootGuard(Decoy):
    def test_an_unsandboxed_request_writes_only_the_test_runtime_root(self):
        override = self.home.parent / 'test-runtime'
        project = self.home / 'project'
        project.mkdir()
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': str(override)}):
            self.assertEqual(runtime_work.request('laya-sync', str(project), start=False),
                             {'status': 'queued'})
            root = override / 'laya-inference'
            # Every participant resolves the same root: queue, coordinator
            # (worker lease, monitor) and the service root itself.
            from context_learning import inference_service, resource_coordination
            self.assertEqual(runtime_work.WakeQueue().root, root)
            self.assertEqual(resource_coordination.Coordinator().root, root)
            self.assertEqual(inference_service.service_root(), root)
            self.assertEqual([row['target'] for row in runtime_work.WakeQueue().pending()],
                             [str(project)])
        self.assertEqual(queues(self.passwd_home), [])
        self.assertEqual(queues(override), [str(root / QUEUE)])

    def test_without_the_override_every_home_keeps_the_per_user_root(self):
        # Production: HOME (temp or a host override) never moves the root.
        for env in ({'MEMVANE_TEST_RUNTIME_ROOT': ''},
                    {'MEMVANE_TEST_RUNTIME_ROOT': '', 'HOME': '/nonexistent-host-home'}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertTrue(runtime_work.WakeQueue().root.is_relative_to(self.passwd_home))

    def test_an_invalid_override_fails_closed_instead_of_using_the_user_root(self):
        project = self.home / 'project'
        project.mkdir()
        for bad in ('relative/root', '/nonexistent-memvane-root', '/tmp', '/usr/share'):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': bad}):
                with self.assertRaises(ValueError):
                    runtime_work.WakeQueue()
                with self.assertRaises(ValueError):
                    runtime_work.request('laya-sync', str(project), start=False)
        self.assertEqual(queues(self.passwd_home), [])

    def test_tmpdir_never_widens_the_trusted_temp_area(self):
        import tempfile
        import runtime_lease
        # A TMPDIR pointing at a real directory (here /usr) is not temp: no
        # test root there, and no immediate drop of a project missing there.
        with mock.patch.object(tempfile, 'tempdir', '/usr'), \
                mock.patch.dict(os.environ, {'TMPDIR': '/usr'}):
            self.assertFalse(runtime_lease.in_platform_temp('/usr/share/memvane-project'))
            import runtime_handlers
            import runtime_wake_policy
            with self.assertRaises(runtime_wake_policy.TargetMissing):
                runtime_handlers.laya_sync({'kind': 'laya-sync',
                                            'target': '/usr/share/memvane-gone-project'})
        self.assertTrue(runtime_lease.in_platform_temp(str(self.home / 'x')))
        self.assertFalse(runtime_lease.in_platform_temp('/tmp'))

    def test_a_real_dispatch_child_resolves_the_same_test_root(self):
        # The sealed-bundle child gets a sanitized environment; it must keep
        # the test runtime root, or its Coordinator() would use the user's.
        import laya_runtime_dispatch
        override = self.home.parent / 'test-runtime'
        # service_root() is what Coordinator() and WakeQueue() resolve; the
        # child only reads it (create=False), so a wrong root is never written.
        code = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                'from context_learning import inference_service as s;'
                'print(json.dumps([str(s.service_root()),str(s.service_root())]))')
        with mock.patch.dict(os.environ, {'MEMVANE_TEST_RUNTIME_ROOT': str(override)}), \
                mock.patch.object(subprocess, 'Popen', REAL_POPEN):
            parent = runtime_work.WakeQueue().root
            child = laya_runtime_dispatch._run_child(
                [sys.executable, '-B', '-c', code, str(CORE)], str(self.home), 60)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout.decode()), [str(parent), str(parent)])
        self.assertEqual(parent, override / 'laya-inference')
        self.assertEqual(queues(self.passwd_home), [])


class SelectedProjectGuard(Decoy):
    def test_selected_project_fixtures_run_sandboxed_inside_their_temp_home(self):
        from laya_selected_project import SelectedProject
        observed = {}

        class Probe(SelectedProject):
            def runTest(probe):
                observed['sandbox'] = os.environ.get('QMD_SANDBOX')
                observed['request'] = runtime_work.request('laya-sync', str(probe.project))
                # Even a test that clears the sandbox stays in its temp HOME.
                with mock.patch.dict(os.environ, {'QMD_SANDBOX': ''}):
                    runtime_work.request('laya-sync', str(probe.project), start=False)
                observed['queues'] = queues(probe.home)
        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertEqual(observed['sandbox'], '1')
        self.assertIsNone(observed['request'])
        self.assertEqual(len(observed['queues']), 1, observed['queues'])
        self.assertEqual(queues(self.passwd_home), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
