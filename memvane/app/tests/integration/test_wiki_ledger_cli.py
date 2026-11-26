"""``entry/run.py wiki-ledger``: read-only ledger views for the dedup skill, resolver agent and logs skill."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from memvane.wiki.pipeline import api as pipeline
from tests.support.case import FixtureCase
from tests.support.project import make_project

REPO = Path(__file__).resolve().parents[4]


class WikiLedgerCli(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root

    def run_cli(self, *args, code=0):
        proc = subprocess.run([sys.executable, '-B', str(REPO / 'entry/run.py'), *args, '--cwd', str(self.root)],
                              capture_output=True, text=True, timeout=60, cwd=str(self.root),
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(proc.returncode, code, proc.stderr)
        return proc.stdout

    def view(self, *args):
        return json.loads(self.run_cli('wiki-ledger', *args, '--json'))

    def test_without_a_ledger_every_view_is_empty_and_nothing_is_created(self):
        for args in (('dedup-pairs',), ('dedup-skip',), ('verify-log',), ('audit', 'candidate')):
            self.assertEqual(self.view(*args), [])
        self.assertFalse(pipeline.pipeline_db_path(self.root).exists())

    def test_dedup_pairs_index_is_the_resolver_index(self):
        pipeline.queue_dedup_pairs(self.root, [{'pageA': 'concepts/b.md', 'pageB': 'concepts/c.md', 'score': 0.93},
                                               {'pageA': 'concepts/a.md', 'pageB': 'concepts/d.md', 'score': 0.91}])
        rows = self.view('dedup-pairs')
        self.assertEqual([sorted(row) for row in rows], [['index', 'pageA', 'pageB', 'score']] * 2)
        self.assertEqual([row['index'] for row in rows], [0, 1])
        before = pipeline.pipeline_db_path(self.root).stat().st_mtime_ns
        self.run_cli('wiki-dedup-resolve', '--index', '0', '--action', 'skip')
        after = self.view('dedup-pairs')
        # The pair at index 0 is gone and the next one moved to index 0.
        self.assertEqual([(r['index'], r['pageA'], r['pageB']) for r in after],
                         [(0, rows[1]['pageA'], rows[1]['pageB'])])
        self.assertGreaterEqual(pipeline.pipeline_db_path(self.root).stat().st_mtime_ns, before)

    def test_dedup_skip_verify_log_and_audit_rows(self):
        self.assertTrue(pipeline.set_suppression(self.root, 'dedup', 'concepts/a.md\nconcepts/b.md', 'h1:h2', {}))
        self.assertEqual(self.view('dedup-skip'), [{'pageA': 'concepts/a.md', 'pageB': 'concepts/b.md',
                                                    'hashes': 'h1:h2'}])
        for n in range(3):
            pipeline.audit_log(self.root, 'verify_log').append({'n': n})
            pipeline.audit_log(self.root, 'candidate').append({'action': 'rejected', 'n': n})
        self.assertEqual(self.view('verify-log', '--limit', '2'), [{'n': 1}, {'n': 2}])
        self.assertEqual([r['n'] for r in self.view('audit', 'candidate')], [0, 1, 2])
        lines = self.run_cli('wiki-ledger', 'audit', 'candidate', '--limit', '1').splitlines()
        self.assertEqual([json.loads(line) for line in lines], [{'action': 'rejected', 'n': 2}])

    def test_audit_needs_exactly_one_kind(self):
        self.run_cli('wiki-ledger', 'audit', code=2)
        self.run_cli('wiki-ledger', 'dedup-pairs', 'candidate', code=2)
        self.run_cli('wiki-ledger', 'audit', 'not-a-kind', code=2)
