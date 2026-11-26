import os
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.wiki.card import api
from memvane.wiki.card import wiki_freshness


class WikiFreshnessTests(FixtureCase):
    def test_card_freshness_tracks_content_and_missing_sources(self):
        project = self.tmp / 'project'
        docs = project / 'docs'
        docs.mkdir(parents=True)
        source = docs / 'source.md'
        source.write_text('original', encoding='utf-8')
        revision = {'kind': 'file', 'path': 'docs/source.md', 'collection': 'raw',
                    **api.snapshot_file(source)}
        card = {'sourceRevisions': [revision]}
        self.assertEqual(api.check_card(card, project, [docs]), {'state': 'fresh', 'checked': 1})
        os.utime(source, (978307200, 978307200))
        self.assertEqual(api.check_card(card, project, [docs]), {'state': 'fresh', 'checked': 1})
        source.write_text('changed', encoding='utf-8')
        self.assertEqual(api.check_card(card, project, [docs]),
                         {'state': 'stale', 'reason': 'content_hash_mismatch'})
        source.unlink()
        self.assertEqual(api.check_card(card, project, [docs]),
                         {'state': 'stale', 'reason': 'missing'})

    def test_unproved_and_outside_sources_remain_unknown(self):
        project = self.tmp / 'project'
        docs = project / 'docs'
        docs.mkdir(parents=True)
        outside = self.tmp / 'outside.md'
        outside.write_text('outside', encoding='utf-8')
        revision = {'kind': 'file', 'path': str(outside), 'collection': 'raw',
                    **api.snapshot_file(outside)}
        for card in ({'sourceRevisions': [revision]}, {'sourceRevisions': []},
                     {'reviewed': True, 'sourceRevisions': []}):
            with self.subTest(card=card):
                self.assertEqual(api.check_card(card, project, [docs])['state'], 'unknown')

    def test_snapshot_hash_detects_content_change(self):
        source = self.tmp / 'source.md'
        source.write_text('before', encoding='utf-8')
        snapshot = api.snapshot_file(source)
        revision = {'kind': 'file', 'path': 'source.md', 'collection': 'raw', **snapshot}
        self.assertEqual(api.compare_revision(source, revision)[0], 'fresh')
        source.write_text('after', encoding='utf-8')
        self.assertEqual(api.compare_revision(source, revision)[0], 'stale')

    def test_source_missing_is_not_fresh(self):
        source = self.tmp / 'missing.md'
        self.assertIsNone(api.snapshot_file(source))

    def test_snapshot_rejects_a_file_changed_during_read(self):
        source = self.tmp / 'source.md'
        source.write_text('original', encoding='utf-8')
        original_fstat = os.fstat
        calls = []

        def changed(fd):
            calls.append(fd)
            if len(calls) == 2:
                source.write_text('changed during read', encoding='utf-8')
            return original_fstat(fd)

        with patch.object(wiki_freshness.os, 'fstat', side_effect=changed):
            self.assertIsNone(api.snapshot_file(source))

    def test_pending_token_requires_current_revision_and_cannot_be_replayed(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        source = root / 'source.md'
        source.write_text('before', encoding='utf-8')
        old = {'kind': 'file', 'path': 'source.md', 'collection': 'raw',
               **api.snapshot_file(source)}
        source.write_text('after', encoding='utf-8')
        first = api.record_pending_refresh(root, 'source.md', 'codex')
        self.assertFalse(wiki_freshness.resolve_pending_refresh(root, first, old, [old]))
        current = {'kind': 'file', 'path': 'source.md', 'collection': 'raw',
                   **api.snapshot_file(source)}
        self.assertTrue(wiki_freshness.resolve_pending_refresh(root, first, current, [current]))
        later = api.record_pending_refresh(root, 'source.md', 'codex')
        self.assertNotEqual(first['eventId'], later['eventId'])
        self.assertFalse(wiki_freshness.resolve_pending_refresh(root, first, current, [current]))
        self.assertEqual([r['eventId'] for r in api.unresolved_pending_refreshes_strict(root)],
                         [later['eventId']])
        self.assertTrue(wiki_freshness.resolve_pending_refresh(root, later, current, [current]))

    def test_resolved_pending_events_are_pruned_to_each_sources_newest(self):
        root = self.tmp / 'project'
        root.mkdir()
        for name in ('source.md', 'other.md'):
            (root / name).write_text(name, encoding='utf-8')
        first = api.record_pending_refresh(root, 'source.md', 'claude')
        source = {'kind': 'file', 'path': 'source.md', 'collection': 'raw',
                  **api.snapshot_file(root / 'source.md')}
        self.assertTrue(wiki_freshness.resolve_pending_refresh(root, first, source, [source]))
        latest = api.record_pending_refresh(root, 'source.md', 'codex')
        other = api.record_pending_refresh(root, 'other.md', 'hermes')
        revision = {'kind': 'file', 'path': 'other.md', 'collection': 'raw',
                    **api.snapshot_file(root / 'other.md')}
        self.assertTrue(wiki_freshness.resolve_pending_refresh(root, other, revision, [revision]))
        self.assertEqual(wiki_freshness.absorb_edit_inbox(root)['files'], 1)
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline(root, readonly=True) as store:
            rows = store.refresh_rows()
        self.assertEqual([(r['event_id'], r['state']) for r in rows],
                         [(latest['eventId'], 'pending'), (other['eventId'], 'resolved')])
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [latest])
        self.assertFalse((root / '.memvane/compile/source-refresh-pending.jsonl').exists())
        self.assertFalse((root / '.memvane/compile/.handoff').exists())

    def test_strict_pending_reader_distinguishes_absent_and_unreadable_ledger(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [])
        event = api.record_pending_refresh(root, 'source.md', 'claude')
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [event])
        (root / '.memvane/compile/wiki-pipeline.sqlite3').write_bytes(b'not a database')
        self.assertIsNone(api.unresolved_pending_refreshes_strict(root))
        self.assertEqual(api.unresolved_pending_refreshes(root), [])

    def test_pending_read_permission_failure_is_unknown(self):
        root = self.tmp / 'project'
        (root / '.memvane' / 'compile').mkdir(parents=True)
        with patch.object(wiki_freshness, '_pending_path', side_effect=PermissionError):
            self.assertIsNone(api.unresolved_pending_refreshes_strict(root))
        api.record_pending_refresh(root, 'source.md', 'claude')
        with patch.object(wiki_freshness, '_read_inbox_file', side_effect=PermissionError):
            self.assertIsNone(api.unresolved_pending_refreshes_strict(root))

    def test_hook_append_respects_lock_timeout_and_never_opens_the_ledger(self):
        import fcntl
        import sqlite3
        root = self.tmp / 'project'
        (root / '.memvane/compile').mkdir(parents=True)
        with patch.object(sqlite3, 'connect', side_effect=AssertionError('hook opened the ledger')):
            for _ in range(3):
                self.assertIsNotNone(api.record_pending_refresh(root, 'source.md', 'claude', compact=False,
                                                                lock_timeout=0.15))
        inbox = root / '.memvane/compile/edit-inbox.jsonl'
        self.assertEqual(len(inbox.read_text().splitlines()), 3)
        fd = os.open(inbox.with_name(inbox.name + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reasons = []
            self.assertIsNone(api.record_pending_refresh(root, 'source.md', 'claude', compact=False,
                                                         lock_timeout=0.05, error_out=reasons))
            self.assertEqual(reasons, ['busy'])
        finally:
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
