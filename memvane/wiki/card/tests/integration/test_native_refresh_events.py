"""A private edit never resolves or damages another ledger's source event."""
import errno
import json
import os
from pathlib import Path
from unittest.mock import patch

from memvane.wiki.card import api as card, refresh_lanes
from tests.support.case import FixtureCase
from tests.support.project import make_project


class NativeEdits(FixtureCase):
    def setUp(self):
        super().setUp()
        project = make_project(self.home, git=False)
        self.root = project.root
        self.revision = project.revision(project.write_note('one.md', 'first revision'))
        self.first = card.record_pending_refresh(self.root, 'one.md', 'codex')
        self.public = self.root / '.memvane/compile/source-refresh-pending.jsonl'
        self.private = refresh_lanes.private_path(self.public)
        self.assertFalse(self.public.exists())

    def test_failed_private_append_keeps_earlier_edit_recoverable(self):
        before = self.private.read_bytes()
        replace = os.replace

        def fail(source, dest):
            if Path(dest) == self.private:
                raise OSError(errno.ENOSPC, 'fixture has no space')
            return replace(source, dest)

        with patch.object(os, 'replace', side_effect=fail):
            self.assertIsNone(card.record_pending_refresh(self.root, 'one.md', 'claude'))
        self.assertEqual(self.private.read_bytes(), before)
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [self.first])
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.private.parent.glob('.edit-*')), [])

    def test_private_event_fields_round_trip_unicode_line_separators(self):
        engine = 'name\u0085part\u2028part\u2029end'
        event = card.record_pending_refresh(self.root, 'one.md', engine)
        self.assertEqual(event['engine'], engine)
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [event])
        self.assertFalse(self.public.exists())

    def test_same_source_in_both_ledgers_requires_the_jobs_own_event(self):
        released = dict(self.first, eventId='1' * 32)
        self.public.write_text(json.dumps(released) + '\n')
        self.assertIsNone(card.latest_unresolved_pending(self.root, 'one.md'))
        for event in (released, self.first):
            self.assertEqual(card.latest_unresolved_pending(self.root, 'one.md', event_id=event['eventId']), event)
        self.assertTrue(card.resolve_pending_refresh(self.root, released, self.revision, [self.revision]))
        self.assertEqual(card.unresolved_pending_refreshes_strict(self.root), [self.first])

    def test_same_id_in_both_ledgers_never_grants_resolution(self):
        self.public.write_text(json.dumps(dict(self.first, sourcePath='other.md')) + '\n')
        before = [p.read_bytes() for p in (self.private, self.public)]
        self.assertIsNone(card.unresolved_pending_refreshes_strict(self.root))
        self.assertFalse(card.resolve_pending_refresh(self.root, self.first, self.revision, [self.revision]))
        self.assertEqual([p.read_bytes() for p in (self.private, self.public)], before)
