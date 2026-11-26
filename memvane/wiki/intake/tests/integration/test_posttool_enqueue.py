"""Edited source admission records refresh evidence before queueing work."""
import json
from unittest.mock import patch

from memvane.wiki.intake import wiki_compile_enqueue as enqueue
from memvane.wiki.card import api as card, types as card_types
from memvane.wiki.workspace import api as workspace, types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class PosttoolEnqueue(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.source = self.project.write_note('docs/source.md', 'synthetic source\n')
        use_config(self, self.project, expanded(self.project, compile={
            'mode': 'auto-wiki', 'triggers': ['post_tool_source']}))
        selected = patch.object(workspace, 'selected', return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        self.public_queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
        self.public_pending = self.root / workspace.rel(card_types.SOURCE_REFRESH_PENDING)
        self.queue = workspace.safe_queue_path(self.root, workspace.rel(workspace_types.SOURCE_QUEUE), native=True)
        self.pending = self.public_pending.with_name('edit-inbox.jsonl')


    def run_edit(self, path, **extra):
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                   'engine': 'codex', 'tool_input': {'file_path': str(path)}, **extra}
        return enqueue.enqueue_payload(payload)

    def rows(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_direct_append_skips_incompatible_sources_before_the_enqueue_limit(self):
        from memvane.wiki.intake import api as intake
        paths = ['docs/a' + sep + '.md' for sep in ('\x85', '\u2028', '\u2029')]
        jobs = [{'source': {'kind': 'file', 'collection': self.project.raw_name, 'path': path}}
                for path in [*paths, 'docs/source.md']]
        intake.append_source_jobs(self.public_queue, jobs[:-1])
        self.assertEqual(self.rows(self.queue), [])
        candidates = [(self.project.raw_name, job['source']['path'], job) for job in jobs]
        enqueue._append_unqueued(self.public_queue, candidates, 1)
        self.assertEqual([r['source']['path'] for r in self.rows(self.queue)], ['docs/source.md'])
        self.assertEqual(self.rows(self.public_queue), [])

    def test_patch_and_edit_array_keep_each_original_path_until_admission(self):
        normal = self.project.write_note('docs/accepted.md', 'body\u2028only\n')
        paths = []
        commands = []
        for sep in ('\x85', '\u2028', '\u2029'):
            ending = 'docs/source.md' + sep
            parent = 'docs/a' + sep + '/note.md'
            paths.extend([ending, parent])
            for name in (ending, parent):
                self.project.write_note(name, 'original bytes\n')
            commands.extend(['*** Add File: ' + ending, '*** Delete File: ' + parent,
                             '*** Update File: ' + ending, '*** Move to: ' + parent])
        payload = {'tool_name': 'apply_patch', 'tool_input': {
            'command': '\r\n'.join(commands + ['*** Update File: docs/accepted.md']),
            'edits': [{'file_path': path} for path in paths]}}
        from memvane.diagnostics import api as diagnostics
        with patch.object(diagnostics, 'emit') as log:
            self.assertEqual(self.run_edit(normal, **payload)['queued'], 1)
        reasons = [call for call in log.call_args_list if
                   call.kwargs.get('reason') == 'compile_source_path_line_separator']
        self.assertEqual(len(reasons), len(paths))
        self.assertEqual([r['source']['path'] for r in self.rows(self.queue)], ['docs/accepted.md'])
        self.assertEqual([r['sourcePath'] for r in self.rows(self.pending)], ['docs/accepted.md'])
        for path in paths:
            self.assertEqual((self.root / path).read_bytes(), b'original bytes\n')

    def test_source_edit_records_pending_refresh_before_its_source_queue_job(self):
        self.assertEqual(self.run_edit(self.source), {'queued': 1, 'reason': 'queued'})
        self.assertFalse(self.public_queue.exists())
        self.assertFalse(self.public_pending.exists())
        pending, jobs = self.rows(self.pending), self.rows(self.queue)
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]['sourcePath'], pending[0]['state'], pending[0]['engine']),
                         ('docs/source.md', 'pending_refresh', 'codex'))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['pendingRefresh'], {key: pending[0][key] for key in ('eventId', 'ts', 'engine')})
        self.assertEqual(jobs[0]['source']['path'], 'docs/source.md')
        self.assertNotIn('content', jobs[0]['source'])

    def test_pending_append_failure_never_queues_an_unguarded_source_job(self):
        original = card.snapshot_file(self.source)
        self.source.write_text('source changed after compilation\n')
        outside = self.home / 'outside.jsonl'
        outside.write_text('untouched\n')
        self.pending.parent.mkdir(parents=True, exist_ok=True)
        self.pending.symlink_to(outside)
        self.assertEqual(self.run_edit(self.source), {'queued': 0, 'reason': 'no_sources'})
        self.assertEqual(self.rows(self.queue), [])
        self.assertEqual(outside.read_text(), 'untouched\n')
        evidence = {'sourceRevisions': [{'kind': 'file', 'path': 'docs/source.md',
                                         'collection': self.project.raw_name, **original}]}
        self.assertEqual(card.check_card(evidence, self.root, [self.root])['state'], 'stale')

    def test_apply_patch_payload_enqueues_source(self):
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root), 'engine': 'codex',
                   'tool_name': 'apply_patch', 'tool_input': {'patch': '*** Begin Patch\n*** Update File: docs/source.md\n@@\n-a\n+b\n*** End Patch'}}
        self.assertEqual(enqueue.enqueue_payload(payload)['queued'], 1)
        self.assertEqual(self.rows(self.queue)[0]['source']['path'], 'docs/source.md')

    def test_non_text_hidden_wiki_and_outside_paths_do_not_enqueue(self):
        paths = [self.project.write_note('docs/code.py', 'code\n'),
                 self.project.write_note('docs/.hidden/note.md', 'hidden\n'),
                 self.project.write_note('.memvane/wiki/generated.md', 'wiki\n'),
                 self.home / 'outside.md']
        paths[-1].write_text('outside\n')
        for path in paths:
            with self.subTest(path=str(path)):
                self.assertEqual(self.run_edit(path)['queued'], 0)
        self.assertEqual(self.rows(self.queue), [])
        self.assertEqual(card.unresolved_pending_refreshes(self.root), [])

    def test_disabled_compile_and_indexing_do_not_enqueue(self):
        variants = ({'compile': {'mode': 'auto-wiki', 'triggers': ['manual']}},
                    {'compile': {'mode': 'off', 'triggers': ['post_tool_source']}},
                    {'indexing': False}, {'indexing': None})
        for overrides in variants:
            with self.subTest(overrides=overrides):
                use_config(self, self.project, expanded(self.project, **overrides))
                self.assertEqual(self.run_edit(self.source)['queued'], 0)
                self.assertEqual(self.rows(self.queue), [])
                self.assertEqual(self.rows(self.pending), [])

    def test_text_extensions_enqueue_but_code_and_dependencies_do_not(self):
        for rel in ('docs/note.txt', 'docs/guide.RST', 'docs/tool.py',
                    'docs/node_modules/pkg/readme.md'):
            path = self.project.write_note(rel, 'synthetic text\n')
            self.run_edit(path)
        self.assertEqual(sorted(row['source']['path'] for row in self.rows(self.queue)),
                         ['docs/guide.RST', 'docs/note.txt'])

    def test_sandbox_dispatch_never_enqueues(self):
        import os
        import subprocess
        from pathlib import Path
        hook = Path(__file__).resolve().parents[5] / 'hooks/run-hook'
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                   'tool_name': 'Write', 'tool_input': {'file_path': str(self.source)}}
        child = subprocess.run([str(hook), 'compile', 'claude'], input=json.dumps(payload), text=True,
                               capture_output=True, env={**os.environ, 'QMD_SANDBOX': '1'}, timeout=30)
        self.assertEqual((child.returncode, child.stdout, child.stderr), (0, '', ''))
        self.assertEqual(self.rows(self.queue), [])
        self.assertEqual(self.rows(self.pending), [])

    def test_five_edits_keep_one_job_with_the_latest_refresh_and_host(self):
        for index in range(5):
            self.source.write_text('revision %d' % index)
            engine = 'claude' if index == 4 else 'codex'
            self.assertEqual(self.run_edit(self.source, engine=engine)['queued'], 1)
        job, = self.rows(self.queue)
        pending = self.rows(self.pending)[-1]
        self.assertEqual(job['pendingRefresh'], {key: pending[key] for key in ('eventId', 'ts', 'engine')})
        self.assertEqual(job['engine'], 'claude')

    def test_pending_only_records_refresh_without_touching_queue_or_selecting_sync_owner(self):
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                   'engine': 'claude', 'tool_input': {'file_path': str(self.source)}}
        with patch.object(workspace, 'selected', side_effect=AssertionError('selected must not run')):
            outcome = enqueue.enqueue_payload(payload, pending_only=True, selected_route=None)
        self.assertEqual(outcome, {'queued': 1, 'failed': 0, 'reason': 'pending_recorded'})
        self.assertFalse(self.queue.exists())
        self.assertFalse(self.public_queue.exists())
        self.assertEqual([(r['sourcePath'], r['engine']) for r in self.rows(self.pending)], [('docs/source.md', 'claude')])

    def test_pending_only_multi_file_shares_single_lock_budget_and_reports_busy(self):
        import fcntl
        extra_a = self.root / 'docs/a.md'
        extra_b = self.root / 'docs/b.md'
        extra_a.write_text('# a\n')
        extra_b.write_text('# b\n')
        lock_path = self.pending.with_name(f'{self.pending.name}.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'hook_event_name': 'PostToolUse', 'cwd': str(self.root), 'engine': 'claude',
            'tool_name': 'apply_patch',
            'tool_input': {'patch': '*** Begin Patch\n*** Update File: docs/source.md\n*** Update File: docs/a.md\n*** Update File: docs/b.md\n*** End Patch'},
        }
        timeouts = []
        real_record = card.record_pending_refresh

        def spy_record(*args, **kwargs):
            timeouts.append(kwargs.get('lock_timeout'))
            return real_record(*args, **kwargs)

        with lock_path.open('a', encoding='utf-8') as lock, \
             patch.object(card, 'record_pending_refresh', side_effect=spy_record):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            outcome = enqueue.enqueue_payload(payload, pending_only=True, selected_route=None)
        self.assertEqual(len(timeouts), 3)
        self.assertGreater(timeouts[0], 0.0)
        self.assertLessEqual(timeouts[0], enqueue.HOOK_PENDING_LOCK_SECONDS)
        self.assertLess(timeouts[1], timeouts[0])
        self.assertLessEqual(timeouts[2], timeouts[1])
        self.assertEqual(outcome, {'queued': 0, 'failed': 3, 'failedReason': 'busy', 'reason': 'pending_busy'})
