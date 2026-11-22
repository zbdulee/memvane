"""The LP-2 driver continues one run across processes (no model: LP-1's fake trainer).

Each call is a new interpreter on the same directory, as the guard runs it. The
first builds the fixture and stops after one pass; the second attaches and
goes on from the rows the first left.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.support.case import FixtureCase
from tests.support.install_builder import ROOT

CALL = ('import json, sys\n'
        'from memvane.learning.tests.model import lp2\n'
        'print(json.dumps(lp2.run(sys.argv[1], seconds=600, real=False, edits=16, max_passes=int(sys.argv[2]))))\n')


class Continued(FixtureCase):
    def call(self, directory, max_passes):
        home = directory / 'h'
        home.mkdir(mode=0o700, exist_ok=True)
        (home / 'rt').mkdir(mode=0o700, exist_ok=True)
        env = dict(os.environ, HOME=str(home), MEMVANE_TEST_RUNTIME_ROOT=str(home / 'rt'),
                   QMD_SETUP_GUARD_FIXTURE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
        env.pop('TMPDIR', None)
        done = subprocess.run([sys.executable, '-B', '-c', CALL, str(directory), str(max_passes)],
                              cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_second_process_attaches_and_goes_on_from_the_rows_of_the_first(self):
        directory = self.home / 'd'
        directory.mkdir(mode=0o700)
        self.assertIsNone(self.call(directory, 1))
        rows = [json.loads(line) for line in (directory / 'lp2-record.jsonl').read_text().splitlines()]
        self.assertEqual([row for row in rows if 'edits' in row], [{'edits': 16, 'stored': 16, 'captures': 16}])
        first, = [row for row in rows if 'pass' in row]
        self.assertEqual((first['pass'], first['status'], first['class'], first['teacherCalls'], first['violations']),
                         (1, 'waiting_fresh_train', 'waiting', 8, 0))
        self.assertEqual(first['counts']['cases'], {'gold': 8, 'queued': 8})
        marker = json.loads((directory / 'lp2-fixture.json').read_text())
        self.assertIsNone(self.call(directory, 3))
        rows = [json.loads(line) for line in (directory / 'lp2-record.jsonl').read_text().splitlines()]
        # No edit is made again; the passes go on with the next number and the queued cases.
        self.assertEqual(len([row for row in rows if 'edits' in row]), 1)
        passes = [row for row in rows if 'pass' in row]
        self.assertEqual([(row['pass'], row['teacherCalls'], row['counts']['cases'], row['violations'])
                          for row in passes[1:]], [(2, 8, {'gold': 16}, 0), (3, 0, {'gold': 16}, 0)])
        self.assertEqual(json.loads((directory / 'lp2-fixture.json').read_text()), marker)
        self.assertEqual([row for row in rows if 'summary' in row], [])
