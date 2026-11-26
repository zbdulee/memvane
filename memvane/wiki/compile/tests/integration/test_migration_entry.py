"""Entrypoints yield to the one-time 1.x cutover the compile worker runs under its run lock (M8, M10)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
from unittest.mock import patch

from memvane.wiki.compile import selected_execution, wiki_compile as compiler, wiki_compile_worker as worker
from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class MigrationEntry(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.config = expanded(self.project, compile={'verify': {'enabled': False},
                                                      'semanticDedup': {'enabled': False}})
        self.compile_dir = self.root / '.memvane/compile'
        self.compile_dir.mkdir(parents=True, exist_ok=True)
        # An abandoned 1.x claim file: discarded state, never read by a decision.
        self.leftover = self.compile_dir / 'source-queue.jsonl.claimed.4242.abcd'
        self.leftover.write_text('{}\n')
        self.bound = ({'state': 'candidate', 'projectRoot': str(self.root),
                       'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                       'recallSettings': {'skipPaths': []},
                       'collections': [{'name': self.project.wiki_name, 'role': 'wiki',
                                        'sourceRoot': str(self.project.wiki)}]}, {'productRouteAllowed': True})
        self.queue = self.compile_dir / 'source-queue.jsonl'

    def skill_compile(self):
        candidate = {'trigger': 'manual', 'title': 'Signal Rule', 'canonicalKey': 'signal-rule',
                     'summary': 'Synthetic durable decision.', 'suggestedType': 'concept',
                     'confidence': 'high', 'sources': [{'kind': 'file', 'path': 'docs/source.md'}]}
        out = io.StringIO()
        with patch('sys.argv', ['wiki-compile', '--cwd', str(self.root)]), \
                patch.object(compiler, 'load_payload', return_value=candidate), \
                patch.object(compiler, 'project_paths', return_value=(self.root, self.config)), \
                patch.object(workspace, 'selected', return_value=self.bound), \
                patch.object(workspace, 'request_compile') as wake, redirect_stdout(out):
            code = compiler.main()
        return code, out.getvalue().strip(), wake

    def test_m10_skill_refuses_and_wakes_and_idle_waits_until_the_worker_migrates(self):
        code, output, wake = self.skill_compile()
        self.assertEqual((code, json.loads(output)), (0, {'action': 'deferred', 'reason': 'pipeline_migration_pending'}))
        wake.assert_called_once_with(self.root, self.bound)
        self.assertFalse((self.project.wiki / 'concepts/signal-rule.md').exists())
        self.assertIs(selected_execution._idle_locked(self.root, self.queue, self.queue, self.bound), False)

        self.assertIs(worker.migrate_v1_state(self.root, self.config), True)
        self.assertFalse(self.leftover.exists())
        self.assertTrue(pipeline.v1_migration_marked(self.root))
        self.assertEqual(len(pipeline.log_rows(self.root, 'migration')), 1)
        self.assertIsNot(selected_execution._idle_locked(self.root, self.queue, self.queue, self.bound), False)
        self.project.write_note('docs/source.md', 'synthetic source\n')
        with patch.object(workspace, 'recheck'), patch.object(workspace, 'request_sync'), \
                patch.object(workspace, 'append_activity'):
            code, output, wake = self.skill_compile()
        self.assertNotEqual(json.loads(output.splitlines()[-1]).get('reason'), 'pipeline_migration_pending')
        wake.assert_not_called()
        # Later passes find the marker and do nothing more.
        self.assertIs(worker.migrate_v1_state(self.root, self.config), True)
        self.assertEqual(len(pipeline.log_rows(self.root, 'migration')), 1)

    def test_m8_a_failed_cutover_claims_nothing_and_the_next_pass_retries(self):
        with patch.object(pipeline, 'migrate_from_v1', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(worker.migrate_v1_state(self.root, self.config), 'failed:OperationalError')
        self.assertTrue(self.leftover.exists())
        self.assertFalse(pipeline.v1_migration_marked(self.root))
        self.assertIs(worker.migrate_v1_state(self.root, self.config), True)
        self.assertFalse(self.leftover.exists())

    def test_m8_the_cutover_runs_inside_the_run_lock_before_any_claim(self):
        source = Path(worker.__file__).read_text()
        main = source[source.index('def _main():'):source.index('def _claim_frozen_work')]
        lock = main.index('run_lock = try_run_lock(root)')
        call = main.index('migrated = migrate_v1_state(root, config)')
        release = main.index('os.close(run_lock)')
        self.assertLess(lock, call)
        self.assertLess(call, main.index('return _main_locked('))
        self.assertLess(call, release)
        self.assertLess(main.index('cp.validate_compile_run_lock(root, run_lock)'), call)

    def test_m10_a_project_without_1x_files_never_waits_and_gets_the_marker(self):
        self.leftover.unlink()
        self.assertFalse(pipeline.needs_v1_migration(self.root))
        self.assertIsNot(selected_execution._idle_locked(self.root, self.queue, self.queue, self.bound), False)
        self.assertFalse(pipeline.v1_migration_marked(self.root))
        self.assertIs(worker.migrate_v1_state(self.root, self.config), True)
        self.assertTrue(pipeline.v1_migration_marked(self.root))
