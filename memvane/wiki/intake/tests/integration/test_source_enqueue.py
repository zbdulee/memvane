"""Source queues preserve ordering, consent, caps and revision coalescing."""
import copy
import json
import os
from unittest.mock import patch

from memvane.wiki.intake import wiki_compile_enqueue as enqueue
from memvane.wiki.workspace import api as workspace
from memvane.wiki.workspace import types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded


class SourceEnqueue(FixtureCase):
    def setUp(self):
        super().setUp()
        limits = patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '64'})
        limits.start()
        self.addCleanup(limits.stop)
        self.project = make_project(self.home, git=False)
        for path in ('docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md',
                     'docs/note.py', 'docs/.draft/hidden.md', 'wiki/w.md'):
            self.project.write_note(path, 'synthetic ' + path + '\n')
        self.config = expanded(self.project, compile={'mode': 'auto-wiki',
            'triggers': ['post_sync_source', 'session_start_source'],
            'extractor': {'builtins': ['claude']}})
        self.config.update(collections=['docs', 'src', 'wiki'],
                           collectionPaths={'docs': 'docs', 'src': 'src', 'wiki': 'wiki'},
                           collectionRoles={'docs': 'raw', 'src': 'source', 'wiki': 'wiki'})
        self.changed = {
            'docs': {'root': str(self.project.root / 'docs'), 'created': ['c.md', 'note.py', '.draft/hidden.md'],
                     'updated': ['b.md', 'a.md'], 'deleted': ['gone.md']},
            'src': {'root': str(self.project.root / 'src'), 'created': ['s.md'], 'updated': [], 'deleted': []},
            'wiki': {'root': str(self.project.root / 'wiki'), 'created': ['w.md'], 'updated': [], 'deleted': []}}
        self.queue = self.project.root / workspace.rel(workspace_types.SOURCE_QUEUE)

    def run_enqueue(self, **kwargs):
        return enqueue.enqueue_changed_sources(str(self.project.root), self.config,
                                               copy.deepcopy(self.changed), engine='claude', **kwargs)

    def rows(self):
        return [json.loads(line) for line in self.queue.read_text().splitlines()] if self.queue.exists() else []

    def test_sync_queues_eligible_sources_in_cap_order(self):
        result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (4, 0, 'queued'))
        rows = self.rows()
        self.assertEqual([(row['source']['collection'], row['source']['path']) for row in rows],
                         [('docs', 'docs/a.md'), ('docs', 'docs/b.md'), ('docs', 'docs/c.md'), ('src', 'src/s.md')])
        self.assertEqual({row['trigger'] for row in rows}, {'post_sync_source'})
        self.assertEqual({row['engine'] for row in rows}, {'claude'})
        self.assertTrue(all('contentRevision' in row and 'content' not in row['source'] for row in rows))

    def test_session_start_records_its_own_trigger(self):
        self.assertEqual(self.run_enqueue(session_start=True)['reason'], 'queued')
        self.assertEqual({row['trigger'] for row in self.rows()}, {'session_start_source'})

    def test_cap_defers_the_tail_and_cap_zero_writes_nothing(self):
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '0'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result['reason']), (0, 4, 'capped'))
        self.assertFalse(self.queue.exists())
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            result = self.run_enqueue()
        self.assertEqual(result['deferredFiles'], [('docs', 'c.md'), ('src', 's.md')])
        self.assertEqual(len(self.rows()), 2)

    def test_trigger_consent_and_non_sources_fail_closed(self):
        self.config['compile']['triggers'] = ['post_tool_source']
        self.assertEqual(self.run_enqueue(session_start=True)['reason'], 'compile_disabled')
        self.assertFalse(self.queue.exists())
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                        'created': ['note.py'], 'updated': ['.draft/hidden.md'], 'deleted': ['a.md']}}
        self.assertEqual(self.run_enqueue()['reason'], 'no_sources')

    def test_same_revision_is_coalesced_but_changed_bytes_requeue(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        again = self.run_enqueue()
        self.assertEqual(again['queued'], 0)
        self.assertTrue(again.get('wakePending'))
        self.project.write_note('docs/a.md', 'changed content\n')
        changed = self.run_enqueue()
        self.assertEqual(changed['queued'], 1)
        self.assertEqual(self.rows()[-1]['source']['path'], 'docs/a.md')

    def test_symlinked_compile_dir_is_rejected_before_planning(self):
        outside = self.home / 'outside'
        outside.mkdir(mode=0o700)
        (self.project.root / '.memvane' / 'compile').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_enqueue()['reason'], 'queue_path_rejected')
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_rows_are_preserved_and_new_rows_append(self):
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.queue.write_text('{"prior": true}\n')
        self.assertEqual(self.run_enqueue()['queued'], 4)
        rows = self.rows()
        self.assertEqual(rows[0], {'prior': True})
        self.assertEqual(len(rows), 5)

    def test_claimed_rows_are_not_suppressed_by_old_queue_keys(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.queue.unlink()
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.assertEqual(len(self.rows()), 4)

    def test_invalid_cap_falls_back_to_default(self):
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': 'many'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['reason']), (4, 'queued'))

    def test_skipped_rows_use_cap_slots_but_are_not_deferred(self):
        only_a = {'docs': {'root': str(self.project.root / 'docs'),
                           'created': [], 'updated': ['a.md'], 'deleted': []}}
        self.changed = only_a
        self.assertEqual(self.run_enqueue()['queued'], 1)
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                                 'created': ['a.md', 'b.md', 'c.md'], 'updated': [], 'deleted': []},
                        'src': {'root': str(self.project.root / 'src'),
                                'created': ['s.md'], 'updated': [], 'deleted': []}}
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            result = self.run_enqueue()
        self.assertEqual((result['queued'], result['deferred'], result.get('wakePending')),
                         (1, 2, True))
        self.assertEqual(result['deferredFiles'], [('docs', 'c.md'), ('src', 's.md')])
        self.assertEqual([r['source']['path'] for r in self.rows()], ['docs/a.md', 'docs/b.md'])

    def test_oversized_live_queue_falls_back_to_plain_append(self):
        self.assertEqual(enqueue.QUEUE_SCAN_MAX_BYTES, 8 * 1024 * 1024)
        self.assertEqual(self.run_enqueue()['queued'], 4)
        original = self.queue.read_bytes()
        self.assertEqual(len(enqueue._queued_revision_keys(self.queue, limit=len(original))), 4)
        self.assertEqual(enqueue._queued_revision_keys(self.queue, limit=len(original) - 1), set())
        with self.queue.open('ab') as handle:
            handle.write(json.dumps({'pad': 'x' * enqueue.QUEUE_SCAN_MAX_BYTES}).encode() + b'\n')
        before = self.queue.read_bytes()
        result = self.run_enqueue()
        self.assertEqual((result['queued'], result['reason']), (4, 'queued'))
        self.assertNotIn('wakePending', result)
        result_bytes = self.queue.read_bytes()
        self.assertTrue(result_bytes.startswith(before))
        before_rows = [json.loads(line) for line in original.splitlines()]
        appended = [json.loads(line) for line in result_bytes[len(before):].splitlines()]
        for row in before_rows + appended:
            row.pop('ts', None)
        self.assertEqual(appended, before_rows)

    def test_same_content_is_queued_once_per_trigger(self):
        import hashlib
        for entry in self.changed.values():
            entry['revisions'] = {rel: hashlib.sha256((self.project.root / entry['root'] / rel).read_bytes()).hexdigest()
                                  for rel in entry['created'] + entry['updated']
                                  if (self.project.root / entry['root'] / rel).is_file()}
        self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
        self.assertEqual(self.run_enqueue(session_start=False)['queued'], 4)
        self.assertEqual(self.run_enqueue(session_start=False)['queued'], 0)
        rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual({row['trigger'] for row in rows},
                         {'session_start_source', 'post_sync_source'})
        self.assertTrue(all(row.get('contentRevision') for row in rows))
