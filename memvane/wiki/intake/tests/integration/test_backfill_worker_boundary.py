"""A frozen backfill job cannot authorize changed source bytes."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from memvane.wiki.intake import wiki_backfill as backfill
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class BackfillWorkerBoundary(FixtureCase):
    def test_worker_does_not_extract_new_bytes_under_frozen_authority(self):
        project = make_project(self.home)
        source = project.write_note('docs/note.md', 'frozen source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
        project.git('commit', '-qm', 'fixture')
        install = build_install(self, project)
        decision = install.select()
        revision = hashlib.sha256(source.read_bytes()).hexdigest()
        snapshot = {project.raw_name + '/docs/note.md': revision}
        config = expanded(project, compile={'mode': 'auto-wiki',
                          'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}})
        row = backfill.discover(decision, snapshot, config, ('sync',), {'sync': 'codex'})
        self.assertIsNotNone(row)
        self.assertEqual(backfill.dispatch(decision, row, snapshot, config, recheck=lambda: None)['dispatched'], 1)
        source.write_text('new bytes require a new grant\n')
        marker = self.home / 'extractor-was-called'
        fake = self.home / 'fake-extractor'
        fake.write_text('#!/usr/bin/env python3\nimport os\nopen(os.environ["FIXTURE_MARKER"], "w").write("called")\nprint("{}")\n')
        fake.chmod(0o700)
        root = Path(__file__).resolve().parents[5]
        env = {**os.environ, 'QMD_ENGINE': 'codex', 'FIXTURE_MARKER': str(marker),
               'QMD_EXTRACTOR_CODEX_BIN': str(fake), 'QMD_EXTRACTOR_CLAUDE_BIN': str(fake),
               'QMD_EXTRACTOR_HERMES_BIN': str(fake)}
        child = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'),
                                'wiki-compile-worker', '--cwd', str(project.root), '--json', '--flush-all'],
                               cwd=str(project.root), env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(marker.exists(), 'changed bytes reached the extractor')
        progress = backfill.summary(backfill._load(decision))
        self.assertEqual((progress['state'], progress['skipped']), ('complete_with_skips', 1))
