"""Guard: a test's runtime wakes stay inside its temp HOME.

The per-user wake queue normally lives under the passwd home, whatever HOME a
host sets. A fixture HOME in the platform temp area gets its own queue, so a
test that clears the sandbox flags still cannot write the real user's queue.
The passwd home is a decoy directory here; nothing outside this test's temp
tree is opened.
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
                            'QMD_SANDBOX': '', 'QMD_RUNTIME_DISPATCH': '1'}),
                        mock.patch.object(runtime_work.subprocess, 'Popen',
                                          side_effect=AssertionError('runtime worker spawned'))):
            patcher.start()
            self.addCleanup(patcher.stop)


class TempHomeGuard(Decoy):
    def test_an_unsandboxed_request_under_a_temp_home_never_writes_the_passwd_home(self):
        project = self.home / 'project'
        project.mkdir()
        self.assertEqual(runtime_work.request('laya-sync', str(project), start=False),
                         {'status': 'queued'})
        self.assertEqual(queues(self.passwd_home), [])
        written = queues(self.home)
        self.assertEqual(len(written), 1, written)
        self.assertTrue(runtime_work.WakeQueue().root.is_relative_to(self.home))
        self.assertEqual([row['target'] for row in runtime_work.WakeQueue().pending()],
                         [str(project)])

    def test_a_real_or_host_home_keeps_the_per_user_queue(self):
        # Production: HOME is the passwd home, or a host override outside the
        # temp area; both keep the one per-user queue.
        for home in (str(self.passwd_home), '/nonexistent-host-home'):
            with self.subTest(home=home), mock.patch.dict(os.environ, {'HOME': home}):
                self.assertIsNone(runtime_work.disposable_home())
                self.assertTrue(runtime_work.WakeQueue().root.is_relative_to(self.passwd_home))


class SelectedProjectGuard(Decoy):
    def test_selected_project_fixtures_run_sandboxed_inside_their_temp_home(self):
        from laya_selected_project import SelectedProject
        observed = {}

        class Probe(SelectedProject):
            def runTest(probe):
                observed['sandbox'] = os.environ.get('QMD_SANDBOX')
                observed['home'] = probe.home
                observed['request'] = runtime_work.request('laya-sync', str(probe.project))
        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertEqual(observed['sandbox'], '1')
        self.assertIsNone(observed['request'])
        self.assertEqual(queues(self.passwd_home), [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
