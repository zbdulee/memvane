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

    def test_pending_refresh_compaction_preserves_latest_unresolved_event(self):
        root = self.tmp / 'project'
        root.mkdir()
        first = api.record_pending_refresh(root, 'source.md', 'claude')
        second = api.record_pending_refresh(root, 'source.md', 'codex')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        pending = api.latest_unresolved_pending(root, 'source.md')
        self.assertEqual(pending['engine'], 'codex')

    def test_strict_pending_reader_distinguishes_absent_and_corrupt(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [])
        api.record_pending_refresh(root, 'source.md', 'claude')
        (root / '.memvane' / 'compile').mkdir(exist_ok=True)
        ledger = next(root.rglob('source-refresh-pending.jsonl'))
        ledger.write_text('{broken\n', encoding='utf-8')
        self.assertIsNone(api.unresolved_pending_refreshes_strict(root))

    def test_strict_pending_reader_rejects_duplicate_and_orphan_events(self):
        root = self.tmp / 'project'
        (root / '.memvane' / 'compile').mkdir(parents=True)
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        pending = {'eventId': 'event-1', 'ts': '2026-08-07T00:00:00Z',
                   'sourcePath': 'docs/source.md', 'state': 'pending_refresh', 'engine': 'codex'}
        import json
        for rows in ([pending, pending], [{'pendingEventId': 'missing',
                                          'ts': '2026-08-07T00:00:01Z', 'state': 'resolved'}],
                     [{'state': 'pending_refresh', 'sourcePath': 'docs/source.md'}]):
            with self.subTest(rows=rows):
                ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                self.assertIsNone(api.unresolved_pending_refreshes_strict(root))
        ledger.write_text(json.dumps(pending) + '\n', encoding='utf-8')
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [pending])

    def test_pending_read_permission_failure_is_unknown(self):
        root = self.tmp / 'project'
        (root / '.memvane' / 'compile').mkdir(parents=True)
        with patch.object(wiki_freshness, '_pending_path', side_effect=PermissionError):
            self.assertIsNone(api.unresolved_pending_refreshes_strict(root))

    def test_valid_pending_ledger_is_never_quarantined(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
        api.record_pending_refresh(root, 'source.md', 'claude')
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        original = ledger.read_bytes()
        self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
        self.assertEqual(ledger.read_bytes(), original)

    def test_corrupt_pending_bytes_are_quarantined_and_preserved(self):
        root = self.tmp / 'project'
        (root / '.memvane' / 'compile').mkdir(parents=True)
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        original = b'\xffbroken\n'
        ledger.write_bytes(original)
        result = wiki_freshness.quarantine_corrupt_pending(root)
        self.assertEqual(result['reason'], 'invalid_utf8')
        self.assertFalse(ledger.exists())
        self.assertEqual((ledger.parent / result['quarantinedTo']).read_bytes(), original)
        self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))


if __name__ == '__main__':
    unittest.main()
