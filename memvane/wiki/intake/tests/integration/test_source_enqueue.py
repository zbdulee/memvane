"""Source queues preserve ordering, consent, caps and revision coalescing."""
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
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
    @property
    def queue(self):
        return workspace.safe_queue_path(self.project.root, workspace.rel(workspace_types.SOURCE_QUEUE), native=True)

    def run_enqueue(self, **kwargs):
        return enqueue.enqueue_changed_sources(str(self.project.root), self.config,
                                               copy.deepcopy(self.changed), engine='claude', **kwargs)

    def rows(self):
        return [job for _, job in workspace.read_queue(self.queue)]

    def seed(self, jobs, *, replace=False):
        """Queue rows as an earlier producer left them (no planning)."""
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline_dir(self.queue.parent) as store:
            if replace:
                store.plan_jobs('source', lambda rows: ([r['jobId'] for r in rows], [], None))
            store.append_jobs('source', jobs)

    def test_session_and_post_sync_exclude_changes_and_deletions_before_selection(self):
        paths = ['a' + sep + '/x.md' for sep in ('\x85', '\u2028', '\u2029')]
        for path in paths:
            self.project.write_note('docs/' + path, 'raw bytes\n')
        entry = {'root': str(self.project.root / 'docs'), 'created': paths[:1], 'updated': paths[1:],
                 'deleted': ['deleted' + path for path in paths], 'revisions': {}}
        entry['revisions'] = {path: 'a' * 64 for path in [*paths, *entry['deleted']]}
        self.changed = {'docs': entry}
        selected = ({'recallSettings': {'skipPaths': []}, 'installId': 'a' * 32,
                     'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32}, {})
        with patch.object(workspace, 'selected', return_value=selected), patch.object(workspace, 'request_compile') as wake:
            for session in (True, False):
                self.assertEqual(self.run_enqueue(session_start=session)['queued'], 0)
            wake.assert_not_called()
            entry['updated'].append('b.md')
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 1)
            self.assertEqual([r['source']['path'] for r in self.rows()], ['docs/b.md'])

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

    def test_sync_keeps_caller_input_and_sources_while_recording_revisions(self):
        import hashlib
        changed = copy.deepcopy(self.changed)
        before = copy.deepcopy(changed)
        source_paths = [self.project.root / path for path in
                        ('docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md')]
        original = {str(path): path.read_bytes() for path in source_paths}
        result = enqueue.enqueue_changed_sources(str(self.project.root), self.config, changed, engine='claude')
        self.assertEqual(result['queued'], 4)
        self.assertEqual(changed, before)
        for row in self.rows():
            path = self.project.root / row['source']['path']
            self.assertEqual(row['contentRevision'], hashlib.sha256(original[str(path)]).hexdigest())
            self.assertEqual(path.read_bytes(), original[str(path)])
        queued = self.rows()
        self.assertEqual(enqueue.enqueue_changed_sources(str(self.project.root), self.config,
                                                         changed, engine='claude')['queued'], 0)
        self.assertEqual(self.rows(), queued)

    def test_manual_sync_queue_record_preserves_fields_and_revision(self):
        import hashlib
        path = self.project.root / 'docs/a.md'
        self.changed = {'docs': {'root': str(path.parent), 'created': ['a.md'],
                                 'updated': [], 'deleted': []}}
        timestamp = '2026-10-17T00:00:00Z'
        with patch.object(enqueue, '_utc_now', return_value=timestamp):
            self.assertEqual(self.run_enqueue()['queued'], 1)
        self.assertEqual(self.rows(), [{
            'ts': timestamp, 'trigger': 'post_sync_source', 'engine': 'claude',
            'cwd': str(self.project.root),
            'source': {'kind': 'file', 'path': 'docs/a.md', 'collection': 'docs'},
            'contentRevision': hashlib.sha256(path.read_bytes()).hexdigest(),
        }])

    def test_session_rows_with_known_revisions_keep_the_other_fields(self):
        import hashlib
        with patch.object(enqueue, '_utc_now', return_value='2026-10-17T00:00:00Z'):
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
            without_revision = self.rows()
            self.seed([], replace=True)
            expected = {}
            for entry in self.changed.values():
                entry['revisions'] = {}
                for rel in entry['created'] + entry['updated']:
                    path = self.project.root / entry['root'] / rel
                    if path.is_file():
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        entry['revisions'][rel] = digest
                        expected[path.relative_to(self.project.root).as_posix()] = digest
            self.assertEqual(self.run_enqueue(session_start=True)['queued'], 4)
        revised = self.rows()
        for row in revised:
            self.assertEqual(row.pop('contentRevision'), expected[row['source']['path']])
        self.assertEqual(revised, without_revision)

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

    def test_changed_revision_replaces_only_its_live_slot(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        again = self.run_enqueue()
        self.assertEqual(again['queued'], 0)
        self.assertTrue(again.get('wakePending'))
        self.project.write_note('docs/a.md', 'changed content\n')
        changed = self.run_enqueue()
        self.assertEqual(changed['queued'], 1)
        rows = self.rows()
        self.assertEqual([r['source']['path'] for r in rows], ['docs/a.md', 'docs/b.md', 'docs/c.md', 'src/s.md'])
        self.assertEqual(rows[0]['contentRevision'], enqueue._stable_revision(self.project.root / 'docs/a.md'))

    def test_symlinked_compile_dir_is_rejected_before_planning(self):
        outside = self.home / 'outside'
        outside.mkdir(mode=0o700)
        (self.project.root / '.memvane' / 'compile').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_enqueue()['reason'], 'queue_path_rejected')
        self.assertEqual(list(outside.iterdir()), [])

    def test_claimed_rows_are_not_suppressed_by_old_queue_keys(self):
        self.assertEqual(self.run_enqueue()['queued'], 4)
        claimed = workspace.claim_queue(self.queue)
        before = [job for _, job in workspace.read_queue(claimed)]
        self.assertEqual(self.run_enqueue()['queued'], 4)
        self.assertEqual(len(self.rows()), 8)  # four claimed plus four queued again
        self.assertEqual([job for _, job in workspace.read_queue(claimed)], before)

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

    def test_edit_collapses_legacy_duplicates_preserving_flags_routes_and_unreadable_lines(self):
        self.changed = {'docs': {'root': str(self.project.root / 'docs'),
                                 'created': [], 'updated': ['a.md'], 'deleted': []}}
        self.run_enqueue()
        current, = self.rows()
        old = dict(current, contentRevision='old')
        separate = [dict(old, **flags) for flags in (
            {'sourceDelete': True}, {'forceRecompile': True},
            {'sourceDelete': True, 'sharedEnqueued': True}, {'initialBackfill': 'a' * 32},
            {'trigger': 'session_start_source'}, {'selectedRoute': {'installId': 'other', 'scopeId': 'scope'}})]
        self.seed([old, current, *separate], replace=True)
        self.assertEqual(self.run_enqueue()['queued'], 1)
        rows = self.rows()
        self.assertEqual(rows[0]['contentRevision'], current['contentRevision'])
        self.assertEqual(rows[1:], separate)

    def test_sync_aliases_share_one_slot_without_acknowledging_a_deferred_file(self):
        docs = self.project.root / 'docs'
        (docs / 'alias.md').symlink_to(docs / 'a.md')
        self.changed = {'docs': {'root': str(docs), 'created': ['a.md', 'alias.md', 'b.md'],
                                 'updated': [], 'deleted': []}}
        with patch.dict(os.environ, {'QMD_SYNC_COMPILE_MAX': '2'}):
            first = self.run_enqueue()
            replay = self.run_enqueue()
        self.assertEqual((first['queued'], first['deferredFiles']), (1, [('docs', 'b.md')]))
        self.assertEqual((replay['queued'], replay['deferredFiles']), (0, [('docs', 'b.md')]))
        self.assertEqual([row['source']['path'] for row in self.rows()], ['docs/a.md'])

    def test_sync_batch_compares_its_last_observation_and_carries_partial_work(self):
        self.run_enqueue()
        current, *neighbors = self.rows()
        older = dict(current, contentRevision='older')
        def put(rows):
            return enqueue._append_unqueued(self.queue, [('docs', 'a.md', row) for row in rows], len(rows))
        self.assertEqual(put([older, current])['result']['queued'], 0)
        self.assertEqual(self.rows(), [current, *neighbors])
        partial = dict(older, partialCompile=True, _processedContentRevision='frozen')
        updated = dict(current, partialCompile=True, _processedContentRevision='frozen')
        self.assertEqual(put([partial, current])['result']['queued'], 1)
        self.assertEqual(self.rows(), [updated, *neighbors])

    def test_producer_and_claimed_worker_keep_independent_operation_occurrences(self):
        row = {'source': {'collection': 'docs', 'path': 'docs/a.md'}, 'contentRevision': 'one'}
        force = dict(row, forceRecompile=True)
        frozen = dict(row, initialBackfill='f' * 32)
        delete = dict(row, sourceDelete=True)
        wait = dict(delete, sharedEnqueued=True)
        expected = [force, force]
        for first in (frozen, delete, wait):
            expected += [first, dict(first, contentRevision='two')]
        # A repeated frozen item is the same cohort/path/revision. Equal force
        # payloads have no parent ID proving the two requests are the same work.
        enqueue._append_jsonl(self.queue, [*expected, frozen])
        self.assertEqual(self.rows(), expected)
        enqueue._append_jsonl(self.queue, [frozen, force])
        expected.append(force)
        self.assertEqual(self.rows(), expected)
        observed = [(json.dumps(item), item) for item in [*self.rows(), frozen]]
        kept, dropped = enqueue.intent.coalesce_claimed_jobs(observed)
        self.assertEqual([item for _, item in kept], expected)
        original_frozen = next(raw for raw, item in observed if item == frozen)
        self.assertEqual(dropped, [original_frozen])
        self.assertEqual(next(raw for raw, item in kept if item == frozen), observed[-1][0])

    def test_one_batch_retains_only_the_last_revision_even_if_already_live(self):
        self.run_enqueue()
        first, *others = self.rows()
        older = dict(first, contentRevision='older')
        enqueue._append_jsonl(self.queue, [older, first])
        self.assertEqual(self.rows(), [first, *others])
        self.seed([], replace=True)
        enqueue._append_jsonl(self.queue, [older, *others, first])
        self.assertEqual(self.rows(), [first, *others])

    def test_failed_atomic_replacement_keeps_original_and_propagates_to_sync(self):
        self.run_enqueue()
        before = self.rows()
        self.project.write_note('docs/a.md', 'replacement')
        with patch.object(enqueue.source_queue.intent, 'plan_source_batch',
                          side_effect=OSError('fixture_write_failure')):
            with self.assertRaisesRegex(OSError, 'fixture_write_failure'):
                self.run_enqueue()
        self.assertEqual(self.rows(), before)
