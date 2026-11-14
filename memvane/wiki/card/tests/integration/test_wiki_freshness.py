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
        import json
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
        self.assertTrue(wiki_freshness.compact_pending_refreshes(root, force=True))
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        rows = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['state'] for row in rows], ['pending_refresh', 'pending_refresh', 'resolved'])
        self.assertEqual(rows[0]['eventId'], latest['eventId'])
        self.assertEqual(rows[1]['eventId'], other['eventId'])
        self.assertEqual(rows[2]['pendingEventId'], other['eventId'])
        self.assertNotRegex(json.dumps(rows), r'sha256|sourceRevision|mtimeNs|"size"|content|secret')
        self.assertFalse(ledger.with_suffix(ledger.suffix + '.compact.tmp').exists())

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
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        ledger.write_text('', encoding='utf-8')
        exists, read_text = Path.exists, Path.read_text
        def denied_exists(path):
            if path == ledger:
                raise PermissionError('synthetic refusal')
            return exists(path)
        def denied_read(path, *args, **kwargs):
            if path == ledger:
                raise PermissionError('synthetic refusal')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'exists', denied_exists):
            self.assertIsNone(api.unresolved_pending_refreshes_strict(root))
        with patch.object(Path, 'read_text', denied_read):
            self.assertIsNone(api.unresolved_pending_refreshes_strict(root))

    def test_valid_pending_ledger_is_never_quarantined(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
        source = root / 'source.md'
        source.write_text('valid', encoding='utf-8')
        pending = api.record_pending_refresh(root, 'source.md', 'claude')
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        revision = {'kind': 'file', 'path': 'source.md', 'collection': 'raw',
                    **api.snapshot_file(source)}
        self.assertTrue(wiki_freshness.resolve_pending_refresh(root, pending, revision, [revision]))
        for content in (ledger.read_bytes(), b'', b'\n'):
            with self.subTest(content=content):
                ledger.write_bytes(content)
                self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
                self.assertEqual(ledger.read_bytes(), content)

    def test_strict_corruption_is_quarantined_without_overwriting_earlier_evidence(self):
        root = self.tmp / 'project'
        (root / '.memvane' / 'compile').mkdir(parents=True)
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        first_bytes = b'{broken one\n'
        second_bytes = b'{broken two\n'
        ledger.write_bytes(first_bytes)
        first = wiki_freshness.quarantine_corrupt_pending(root)
        self.assertEqual(first['reason'], 'strict_reject')
        self.assertFalse(ledger.exists())
        self.assertEqual((ledger.parent / first['quarantinedTo']).read_bytes(), first_bytes)
        self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
        ledger.write_bytes(second_bytes)
        second = wiki_freshness.quarantine_corrupt_pending(root)
        self.assertNotEqual(first['quarantinedTo'], second['quarantinedTo'])
        self.assertEqual((ledger.parent / first['quarantinedTo']).read_bytes(), first_bytes)
        self.assertEqual((ledger.parent / second['quarantinedTo']).read_bytes(), second_bytes)
        self.assertEqual(api.unresolved_pending_refreshes_strict(root), [])

    def test_unreadable_ledger_is_not_quarantined(self):
        root = self.tmp / 'project'
        (root / '.memvane').mkdir(parents=True)
        api.record_pending_refresh(root, 'source.md', 'claude')
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        original = ledger.read_bytes()
        read_text = Path.read_text
        def denied(path, *args, **kwargs):
            if path == ledger:
                raise PermissionError('synthetic refusal')
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied):
            self.assertIsNone(wiki_freshness.quarantine_corrupt_pending(root))
        self.assertEqual(ledger.read_bytes(), original)

    def test_quarantine_cli_exits_success_for_corrupt_and_empty_ledger(self):
        import json
        import subprocess
        import sys
        root = self.tmp / 'project'
        ledger = root / '.memvane' / 'compile' / 'source-refresh-pending.jsonl'
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{broken\n', encoding='utf-8')
        command = [sys.executable, '-B', '-m', 'memvane.wiki.card.wiki_freshness',
                   '--cwd', str(root), '--quarantine-corrupt-pending', '--json']
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'], 'strict_reject')
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})

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
