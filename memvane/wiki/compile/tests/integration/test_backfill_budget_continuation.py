"""A mixed-host compile pass bounds attempts and requests its continuation."""
import argparse
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.engines import api as engines
from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class BackfillBudgetContinuation(FixtureCase):
    def test_per_run_extractor_cap_preserves_and_drains_all_sources(self):
        project = make_project(self.home)
        paths = ['docs/%02d.md' % number for number in range(7)]
        for path in paths:
            project.write_note(path, 'synthetic source ' + path + '\n')
        project.git('add', '--', '.memvane/settings.json', 'docs')
        project.git('commit', '-qm', 'fixture')
        build_install(self, project)
        config = expanded(project, compile={'budget': {'extractorPerRun': 3},
                          'extractor': {'builtins': ['claude']}})
        use_config(self, project, config)
        changed = {project.raw_name: {'root': str(project.root), 'created': paths,
                                      'updated': [], 'deleted': []}}
        with patch.object(workspace, 'request_compile'):
            self.assertEqual(intake.enqueue_changed_sources(str(project.root), config, changed,
                                                            engine='claude')['queued'], 7)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        bound = workspace.selected(project.root)
        calls, reports = [], []
        def extract(argv, payload, timeout, root):
            calls.append(payload['source']['path'])
            return {'candidates': []}, None, 0
        with patch.object(engines, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), patch.object(workspace, 'request_compile'):
            for _ in range(3):
                output = io.StringIO()
                with redirect_stdout(output):
                    worker._main_locked(argparse.Namespace(json=True, flush_all=True), project.root,
                                        config, config['compile'], queue, bound)
                reports.append(json.loads(output.getvalue()))
        self.assertEqual([row['processed'] for row in reports], [3, 3, 1])
        self.assertEqual([row['remaining'] for row in reports], [4, 1, 0])
        self.assertEqual(sorted(calls), sorted(paths))
        self.assertFalse(queue.read_text().strip())

    def test_cards_per_source_cap_records_every_excess_candidate(self):
        project = make_project(self.home)
        project.write_note('docs/note.md', 'synthetic source\n')
        project.git('add', '--', '.memvane/settings.json', 'docs')
        project.git('commit', '-qm', 'fixture')
        build_install(self, project)
        config = expanded(project, compile={'budget': {'cardsPerSource': 4},
                          'extractor': {'builtins': ['claude']}, 'verify': {'enabled': False}})
        use_config(self, project, config)
        changed = {project.raw_name: {'root': str(project.root), 'created': ['docs/note.md'],
                                      'updated': [], 'deleted': []}}
        with patch.object(workspace, 'request_compile'):
            self.assertEqual(intake.enqueue_changed_sources(str(project.root), config, changed,
                                                            engine='claude')['queued'], 1)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        bound, compiled = workspace.selected(project.root), []
        candidates = [{'title': 'Candidate %d' % n, 'summary': 'Synthetic fact'} for n in range(14)]
        def write(root, candidate, **kwargs):
            compiled.append(candidate['title'])
            return {'action': 'created', 'sourceRevisions': candidate['sourceRevisions']}
        with patch.object(engines, 'run_extractor', return_value=({'candidates': candidates}, None, 0)), \
             patch.object(worker, 'compile_candidate', side_effect=write), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), patch.object(workspace, 'request_compile'), \
             redirect_stdout(io.StringIO()):
            worker._main_locked(argparse.Namespace(json=True, flush_all=True), project.root,
                                config, config['compile'], queue, bound)
        self.assertEqual(compiled, ['Candidate %d' % n for n in range(4)])
        audit = project.root / '.memvane/compile/candidates.jsonl'
        rows = [json.loads(line) for line in audit.read_text().splitlines()]
        capped = next(row for row in rows if row.get('reason') == 'cards_per_source_cap')
        self.assertEqual((capped['cardsReturned'], capped['cardsCompiled']), (14, 4))
        self.assertEqual(len(capped['skippedTitles']), 10)
        self.assertEqual(capped['source']['path'], 'docs/note.md')

    def test_mixed_host_attempts_are_bounded_and_request_a_continuation(self):
        project = make_project(self.home)
        paths = ['docs/%02d.md' % number for number in range(10)]
        for path in paths:
            project.write_note(path, 'synthetic source ' + path + '\n')
        project.git('add', '--', '.memvane/settings.json', 'docs')
        project.git('commit', '-qm', 'fixture')
        build_install(self, project)
        config = expanded(project, compile={'mode': 'auto-wiki', 'triggers': ['post_sync_source'],
                          'extractor': {'builtins': ['claude', 'codex', 'hermes']}})
        use_config(self, project, config)
        changed = {project.raw_name: {'root': str(project.root), 'created': paths,
                                      'updated': [], 'deleted': []}}
        with patch.object(workspace, 'request_compile'):
            self.assertEqual(intake.enqueue_changed_sources(str(project.root), config, changed,
                                                            engine='unknown')['queued'], 10)
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        jobs = [json.loads(line) for line in queue.read_text().splitlines()]
        for index, job in enumerate(jobs):
            job['engine'] = 'claude' if index < 2 else 'unknown'
        queue.write_text(''.join(json.dumps(job) + '\n' for job in jobs))
        calls = []
        def extract(argv, payload, timeout, root):
            calls.append(payload['engine'])
            return ({'candidates': []}, None, 0) if len(calls) <= 2 else (None, 'extractor_failed', 1)
        bound = workspace.selected(project.root)
        output = io.StringIO()
        with patch.object(engines, 'run_extractor', side_effect=extract), \
             patch.object(worker, 'gather_similar_pages', return_value=[]), \
             patch.object(worker, '_run_verify_pass'), \
             patch.object(workspace, 'request_compile') as wake, redirect_stdout(output):
            worker._main_locked(argparse.Namespace(json=True, flush_all=True), project.root,
                                config, config['compile'], queue, bound)
        report = json.loads(output.getvalue())
        self.assertEqual(len(calls), 10)
        self.assertTrue(report['budgetDeferred'])
        self.assertGreater(report['remaining'], 0)
        wake.assert_called_once_with(project.root, bound)
        retained = [json.loads(line) for line in queue.read_text().splitlines()]
        receipts = [job['_fallbackAttempts'] for job in retained if '_fallbackAttempts' in job]
        self.assertTrue(receipts)
        self.assertTrue(all(len(receipt['engines']) < 3 for receipt in receipts))
