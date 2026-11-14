"""Ineligible counts distinguish unmeasured, measured zero and full totals."""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from memvane.wiki.lifecycle import wiki_lifecycle as lifecycle
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class IneligibleCounts(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.config = expanded(self.project)
        use_config(self, self.project, self.config)

    def write_card(self, path, *, status='generated', created_by='qmd-auto-context', source=False):
        target = self.project.wiki / path
        target.parent.mkdir(parents=True, exist_ok=True)
        revision = ('sourceRevisions:\n'
                    '  - {kind: "file", path: "docs/a.md", collection: "fixture", '
                    'sha256: "' + 'a' * 64 + '", size: 1, mtimeNs: 1}\n') if source else ''
        target.write_text('---\ntitle: Card\nstatus: ' + status + '\ncreatedBy: ' + created_by
                          + '\n' + revision + '---\n\n<!-- qmd:auto:start id="main" sourceHash="a" -->\n'
                          + 'fact\n<!-- qmd:auto:end -->\n', encoding='utf-8')
        return target

    def run_lifecycle(self):
        with patch.object(workspace, 'selected', return_value=None):
            return lifecycle.run(str(self.root))

    def test_ineligible_counts_match_recall_trust_requirements(self):
        self.write_card('entities/ok.md', status='verified', source=True)
        self.write_card('entities/no-rev.md', status='verified')
        self.write_card('entities/draft.md')
        self.write_card('entities/foreign.md', status='verified', created_by='human', source=True)
        result = self.run_lifecycle()
        self.assertEqual((result['cards'], result['ineligible'], result['ineligibleMeasured']),
                         (4, 3, True))

    def test_scaffold_pages_are_not_counted_or_changed(self):
        paths = []
        for name in ('SCHEMA.md', 'index.md', 'log.md'):
            path = self.project.wiki / name
            path.write_text('# Scaffold\n', encoding='utf-8')
            paths.append(path)
        result = self.run_lifecycle()
        self.assertEqual((result['cards'], result['ineligible']), (0, 0))
        self.write_card('entities/ok.md', status='verified', source=True)
        result = self.run_lifecycle()
        self.assertEqual((result['cards'], result['ineligible']), (1, 0))
        self.assertTrue(all(path.read_text() == '# Scaffold\n' for path in paths))

    def test_disabled_scan_still_measures_ineligible_cards(self):
        config = expanded(self.project, compile={'sourceScan': {'enabled': False}})
        use_config(self, self.project, config)
        self.write_card('entities/ok.md', status='verified', source=True)
        self.write_card('entities/draft.md')
        self.write_card('entities/no-rev.md', status='verified')
        result = self.run_lifecycle()
        self.assertEqual((result['cards'], result['ineligible'], result['ineligibleMeasured'],
                          result['examined']), (3, 2, True, 0))

    def test_unsafe_wiki_is_unmeasured_and_missing_wiki_is_measured_zero(self):
        unsafe = expanded(self.project, wikiPath='../outside')
        use_config(self, self.project, unsafe)
        result = self.run_lifecycle()
        self.assertEqual((result['ineligible'], result['ineligibleMeasured']), (0, False))
        use_config(self, self.project, expanded(self.project))
        self.project.wiki.rmdir()
        result = self.run_lifecycle()
        self.assertEqual((result['ineligible'], result['ineligibleMeasured']), (0, True))

    def test_ineligible_total_is_not_limited_to_cursor_window(self):
        for index in range(5):
            self.write_card('entities/c%d.md' % index)
        with patch.dict(os.environ, {'QMD_SOURCE_SCAN_MAX': '1'}):
            result = self.run_lifecycle()
        self.assertEqual(result['ineligible'], 5)

    def test_sandbox_and_disabled_indexing_preserve_cards(self):
        card = self.write_card('entities/draft.md')
        config = expanded(self.project, indexing=False)
        use_config(self, self.project, config)
        result = self.run_lifecycle()
        self.assertFalse(result['ineligibleMeasured'])
        entry = Path(__file__).resolve().parents[5] / 'entry/run.py'
        env = {**os.environ, 'QMD_SANDBOX': '1'}
        child = subprocess.run([sys.executable, '-B', str(entry), 'wiki-lifecycle',
                                '--cwd', str(self.root), '--json'],
                               env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((child.returncode, child.stdout), (0, ''))
        self.assertTrue(card.exists())
