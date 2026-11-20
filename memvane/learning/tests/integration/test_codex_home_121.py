"""The owned worker keeps the session's CODEX_HOME, so the Codex teacher uses that account."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memvane.learning.context_learning import shared, teacher_hosts
from memvane.learning.context_learning import teacher_isolation as isolation


class OwnedWorkerCodexHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='codex-home-')).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root, ignore_errors=True))
        self.codex_home = self.root / 'codex-account'
        self.codex_home.mkdir(mode=0o700)

    def spawn_env(self, extra):
        seen = []

        def spawn(argv, **options):
            seen.append(options['env'])
            return mock.Mock(pid=4242)

        with mock.patch.dict(os.environ, extra):
            shared._spawn_owned(self.root, self.root, 'run-owned-worker', 9, spawn,
                                job={'MEMVANE_JOB': '1'}, job_id='job-1')
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_codex_home_reaches_the_worker_and_the_teacher_child(self):
        env = self.spawn_env({'CODEX_HOME': str(self.codex_home), 'MEMVANE_TEACHER': 'off'})
        self.assertEqual(env['CODEX_HOME'], str(self.codex_home))
        # Only the whitelist crosses: other session variables do not.
        self.assertNotIn('MEMVANE_TEACHER', env)
        child = teacher_hosts.child_environment('codex', env)
        self.assertEqual(child['CODEX_HOME'], str(self.codex_home))
        isolation.check_env('codex', child, parent=env)
        self.assertNotIn('CODEX_HOME', teacher_hosts.child_environment('claude', env))

    def test_without_codex_home_the_worker_has_none(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('CODEX_HOME', None)
            env = self.spawn_env({})
        self.assertNotIn('CODEX_HOME', env)


if __name__ == '__main__':
    unittest.main()
