"""The hook edit inbox: append-only, absorbed once by the worker, never torn."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.card import api as card, wiki_freshness
from memvane.wiki.pipeline import api as pipeline
from tests.support.case import FixtureCase
from tests.support.project import make_project


class EditInbox(FixtureCase):
    def setUp(self):
        super().setUp()
        project = make_project(self.home, git=False)
        self.root = project.root.resolve()
        self.revision = project.revision(project.write_note('one.md', 'first revision'))
        self.first = card.record_pending_refresh(self.root, 'one.md', 'codex')
        self.inbox = self.root / '.memvane/compile/edit-inbox.jsonl'

    def rows(self):
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            return store.refresh_rows()

    def test_inbox_row_shape_and_unicode_round_trip(self):
        engine = 'name\u0085part part end'
        event = card.record_pending_refresh(self.root, 'one.md', engine)
        self.assertEqual(event['engine'], engine)
        stored = [json.loads(line) for line in self.inbox.read_bytes().splitlines()]
        self.assertEqual(stored[-1], {**event, 'schema': 'memvane-edit-inbox-v1'})
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [event])

    def test_the_release_contract_row_reads_the_same_as_the_writer_row(self):
        """release.md's inbox required keys are exactly what the reader needs from a writer row."""
        required = ('schema', 'state', 'eventId', 'sourcePath', 'ts', 'engine')
        contract = Path(__file__).resolve().parents[5] / 'docs/dev/contexts/release.md'
        line, = [row for row in contract.read_text(encoding='utf-8').splitlines()
                 if '`memvane-edit-inbox-v1`' in row and row.startswith('- ')]
        for key in required:
            self.assertIn(f'`{key}`', line)
        self.assertIn('`"pending_refresh"`', line)
        written = json.loads(self.inbox.read_bytes().splitlines()[-1])
        documented = {key: written[key] for key in required}
        expected = wiki_freshness._inbox_event(json.dumps(written).encode())
        self.assertIsNotNone(expected)
        self.assertEqual(wiki_freshness._inbox_event(json.dumps(documented).encode()), expected)
        self.assertEqual(wiki_freshness._inbox_event(
            json.dumps({**documented, 'privateKey': 1}).encode()), expected, 'other keys are not read')
        for key in required:
            with self.subTest(missing=key):
                row = {k: v for k, v in documented.items() if k != key}
                self.assertIsNone(wiki_freshness._inbox_event(json.dumps(row).encode()))
        for key, value in (('state', 'resolved'), ('eventId', ''), ('sourcePath', '../x.md'),
                           ('ts', '2026-11-26T00:00:00'), ('engine', '')):
            with self.subTest(invalid=key):
                row = {**documented, key: value}
                self.assertIsNone(wiki_freshness._inbox_event(json.dumps(row).encode()))
        self.assertIsNotNone(wiki_freshness._inbox_event(
            json.dumps({**documented, 'eventId': 'not-hex', 'ts': '2026-11-26T09:00:00+09:00'}).encode()),
            'the reader takes any non-empty eventId and any zoned ISO 8601 time')

    def test_absorb_once_across_both_crash_windows(self):
        from tests.support.wiki_handoff import failing_ledger_method
        # I2: stopped after the rename, before the insert commits.
        with failing_ledger_method('absorb_refresh_events', RuntimeError('stop')), \
                self.assertRaises(RuntimeError):
            wiki_freshness.absorb_edit_inbox(self.root)
        self.assertFalse(self.inbox.exists())
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [self.first])
        second = card.record_pending_refresh(self.root, 'two.md', 'claude')
        # I3: stopped after the commit, before the aside file is removed.
        unlink = Path.unlink
        def stop_once(path, *args, **kwargs):
            if wiki_freshness.CONSUMING in path.name:
                raise RuntimeError('stop after commit')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', stop_once), self.assertRaises(RuntimeError):
            wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual(len(self.rows()), 2)
        result = wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual(result['absorbed'], 0)
        self.assertEqual(sorted(r['event_id'] for r in self.rows()), sorted([self.first['eventId'], second['eventId']]))
        self.assertEqual(list(self.inbox.parent.glob('edit-inbox.jsonl.consuming-*')), [])
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [self.first, second])

    def test_full_inbox_is_busy_and_short_write_leaves_no_torn_line(self):
        before = self.inbox.read_bytes()
        reasons = []
        with patch.object(wiki_freshness, 'INBOX_MAX_BYTES', len(before)):
            self.assertIsNone(card.record_pending_refresh(self.root, 'one.md', 'claude', error_out=reasons))
        self.assertEqual(reasons, ['busy'])
        write = os.write
        with patch.object(os, 'write', side_effect=lambda fd, data: write(fd, data[:7])):
            self.assertIsNone(card.record_pending_refresh(self.root, 'one.md', 'claude'))
        self.assertEqual(self.inbox.read_bytes(), before)

    def test_malformed_line_is_counted_and_the_rest_absorbed(self):
        with self.inbox.open('ab') as stream:
            stream.write(b'{broken\n')
        second = card.record_pending_refresh(self.root, 'two.md', 'claude')
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [self.first, second])
        result = wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual((result['absorbed'], result['malformed']), (2, 1))

    def test_same_second_events_keep_only_the_newest_unresolved(self):
        again = card.record_pending_refresh(self.root, 'one.md', 'claude')
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [again])
        self.assertFalse(card.resolve_pending_refresh(self.root, self.first, self.revision, [self.revision]))
        wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual([r['event_id'] for r in self.rows()], [again['eventId']])
        self.assertTrue(card.resolve_pending_refresh(self.root, again, self.revision, [self.revision]))
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [])

    def test_concurrent_hook_appends_during_absorption_lose_nothing(self):
        script = """
import sys
from pathlib import Path
from memvane.wiki.card import api as card
root, tag = Path(sys.argv[1]), sys.argv[2]
for index in range(40):
    while card.record_pending_refresh(root, f'{tag}-{index}.md', tag, lock_timeout=5) is None:
        pass
"""
        children = [subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root), tag])
                    for tag in ('claude', 'codex', 'hermes')]
        while any(child.poll() is None for child in children):
            wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual([child.returncode for child in children], [0, 0, 0])
        wiki_freshness.absorb_edit_inbox(self.root)
        sources = {r['source_path'] for r in self.rows()}
        self.assertEqual(len(sources), 1 + 3 * 40)

    def test_a_resolved_newer_event_stays_newest_over_an_unabsorbed_older_one(self):
        newer = card.record_pending_refresh(self.root, 'one.md', 'claude')
        self.assertTrue(card.resolve_pending_refresh(self.root, newer, self.revision, [self.revision]))
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [])
        wiki_freshness.absorb_edit_inbox(self.root)
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [])
        self.assertEqual([(r['event_id'], r['state']) for r in self.rows()], [(newer['eventId'], 'resolved')])
