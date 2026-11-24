"""A queued old CLI resumes after a transition owner dies, including with a real old tombstone."""
import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.compile.tests.integration.test_pending_handoff import archived_tag
from tests.support.case import FixtureCase


class LegacyTransitionRace(FixtureCase):
    def test_waiting_released_writers_resume_on_owner_exit(self):
        repo = Path(__file__).resolve().parents[5]
        script = repo / 'memvane/wiki/compile/tests/tagged_transition_race.py'
        for tag in ('v1.0.22', 'v1.0.23', 'v1.0.24', 'v1.1.0'):
            bundle = archived_tag(repo, self.tmp, tag)
            for mode, expected in (('no-tombstone', 'created'), ('has-tombstone', 'suppressed')):
                with self.subTest(tag=tag, mode=mode):
                    scratch = self.tmp / (tag + mode)
                    home = scratch / 'home'
                    runtime = home / 'rt'
                    runtime.mkdir(parents=True, mode=0o700)
                    env = {**os.environ, 'HOME': str(home), 'MEMVANE_TEST_RUNTIME_ROOT': str(runtime),
                           'PYTHONPATH': os.pathsep.join((str(bundle), str(repo)))}
                    child = subprocess.run([sys.executable, '-B', str(script), mode, str(scratch)],
                                           env=env, cwd=bundle, capture_output=True, text=True, timeout=40)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(json.loads(child.stdout), expected)
