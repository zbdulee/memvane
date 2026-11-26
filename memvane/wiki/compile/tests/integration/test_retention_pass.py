"""The compile worker runs one bounded ledger retention pass under its run lock (RT9)."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.pipeline import api as pipeline
from tests.support.case import FixtureCase
from tests.support.project import make_project


class RetentionPass(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.wiki = self.project.wiki

    def test_rt9_the_worker_passes_raw_roots_and_ignores_a_failed_pass(self):
        bound = ({'collections': [{'role': 'raw', 'sourceRoot': str(self.root)},
                                  {'role': 'wiki', 'sourceRoot': str(self.wiki)}]}, {})
        with patch.object(pipeline, 'maintain', return_value={}) as maintain:
            worker._retention_pass(self.root, {'wikiPath': '.memvane/wiki'}, bound)
        maintain.assert_called_once_with(self.root, wiki_root=self.wiki.resolve(), source_roots=[self.root])
        with patch.object(pipeline, 'maintain', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(worker._retention_pass(self.root, {}, None), {})
        source = Path(worker.__file__).read_text()
        body = source[source.index('def _main_locked'):]
        self.assertLess(body.index('_retention_pass(root, config, selected_route)'), body.index('claim_queue('))
