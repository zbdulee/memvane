"""CODEX_HOME through the owned worker: the default home passes, a redirected one
only rests the Codex teacher and never stops Claude work."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memvane.learning.context_learning import shared, teacher_hosts
from memvane.learning.context_learning import teacher_isolation as isolation
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture


class OwnedWorkerCodexHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='codex-home-')).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root, ignore_errors=True))
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)

    def spawn_env(self, extra):
        seen = []

        def spawn(argv, **options):
            seen.append(options['env'])
            return mock.Mock(pid=4242)

        with mock.patch.dict(os.environ, dict(extra, HOME=str(self.home))):
            shared._spawn_owned(self.root, self.root, 'run-owned-worker', 9, spawn,
                                job={'MEMVANE_JOB': '1'}, job_id='job-1')
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_default_codex_home_reaches_the_worker_and_the_teacher_child(self):
        default = str(self.home / '.codex')
        env = self.spawn_env({'CODEX_HOME': default, 'MEMVANE_TEACHER': 'off'})
        self.assertEqual(env['CODEX_HOME'], default)
        self.assertNotIn(shared.CODEX_HOME_REDIRECTED, env)
        # Only the whitelist crosses: other session variables do not.
        self.assertNotIn('MEMVANE_TEACHER', env)
        child = teacher_hosts.child_environment('codex', env)
        self.assertEqual(child['CODEX_HOME'], default)
        isolation.check_env('codex', child, parent=env)
        self.assertNotIn('CODEX_HOME', teacher_hosts.child_environment('claude', env))

    def test_redirected_codex_home_stays_out_and_marks_the_worker(self):
        env = self.spawn_env({'CODEX_HOME': str(self.root / 'other-account')})
        # The host inventory refuses a redirected home for every host, so it never
        # reaches the worker; the marker rests only the Codex teacher.
        self.assertNotIn('CODEX_HOME', env)
        self.assertEqual(env[shared.CODEX_HOME_REDIRECTED], '1')
        self.assertNotIn(shared.CODEX_HOME_REDIRECTED, teacher_hosts.child_environment('claude', env))

    def test_without_codex_home_the_worker_has_none(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('CODEX_HOME', None)
            env = self.spawn_env({})
        self.assertNotIn('CODEX_HOME', env)
        self.assertNotIn(shared.CODEX_HOME_REDIRECTED, env)


class RedirectedCodexHomePass(TeacherFixture):
    def test_claude_cases_are_labeled_while_codex_rests(self):
        self.environ[shared.CODEX_HOME_REDIRECTED] = '1'
        self.add('codex-1', host='codex', tag='c')
        self.add('claude-1', host='claude', tag='l')
        result = self.run_pass()
        self.assertEqual(result['hosts']['codex'], teacher_hosts.CODEX_HOME_REDIRECTED)
        self.assertEqual(result['hosts']['claude'], 'ready')
        cases = self.cases()
        self.assertEqual(cases['claude-1'][0], 'gold')
        self.assertEqual(cases['codex-1'][0], 'queued')
        self.assertEqual(self.runs('codex'), 0)
        self.assertEqual(self.runs('claude') > 0, True)


if __name__ == '__main__':
    unittest.main()
