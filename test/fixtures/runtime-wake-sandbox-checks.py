"""Guard: a test's runtime wakes never reach the user's runtime root.

The per-user runtime root (wake queue, worker lease, coordinator, monitor)
lives under the passwd home, whatever HOME a host sets. A test run sets
``MEMVANE_TEST_RUNTIME_ROOT`` (``npm test`` through the development guard, the
shared Laya fixtures in their temp HOME), and every participant resolves that
root instead, so a test that clears the sandbox flags still cannot write the
user's queue. The passwd home is a decoy directory here; nothing outside this
test's temp tree is opened.
"""
import os
from pathlib import Path
import pwd
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
        # Production: HOME (temp or a host override) never moves the root;
        # an override outside the platform temp area is ignored.
        for env in ({'MEMVANE_TEST_RUNTIME_ROOT': ''},
                    {'MEMVANE_TEST_RUNTIME_ROOT': '/nonexistent-memvane-root'},
                    {'MEMVANE_TEST_RUNTIME_ROOT': '', 'HOME': '/nonexistent-host-home'}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertTrue(runtime_work.WakeQueue().root.is_relative_to(self.passwd_home))


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
