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
        self.queue = self.root / workspace.rel(workspace_types.SOURCE_QUEUE)
        self.pending = self.root / workspace.rel(card_types.SOURCE_REFRESH_PENDING)

    def run_edit(self, path, **extra):
        payload = {'hook_event_name': 'PostToolUse', 'cwd': str(self.root),
                   'engine': 'codex', 'tool_input': {'file_path': str(path)}, **extra}
        return enqueue.enqueue_payload(payload)

    def rows(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_source_edit_records_pending_refresh_before_its_source_queue_job(self):
        self.assertEqual(self.run_edit(self.source), {'queued': 1, 'reason': 'queued'})
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
        self.pending.parent.mkdir(parents=True)
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
