"""Rendering a verification stamp is deterministic and does not mutate its input."""
from datetime import datetime, timezone

from memvane.wiki.verify import verification_stamp
from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.settings import api as settings
from memvane.wiki.workspace import types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project


class VerificationRender(FixtureCase):
    def test_fixed_time_renders_deterministically_without_writing_card(self):
        card = self.tmp / 'card.md'
        original = '---\ntitle: Test\nstatus: generated\n---\nbody\n'
        card.write_text(original)
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        first = verification_stamp.render_verification(card, 'verified', 'claude', 'self', 'a' * 64, None, now)
        second = verification_stamp.render_verification(card, 'verified', 'claude', 'self', 'a' * 64, None, now)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertIn('2026-01-02T03:04:05.000000Z', first)
        self.assertEqual(card.read_text(), original)

    def test_unsafe_ledger_paths_fall_back_to_fixed_project_locations(self):
        project = make_project(self.home, git=False)
        directory = project.root / '.memvane/compile'
        directory.mkdir()
        outside = self.home / 'outside.jsonl'
        outside.write_text('unchanged')
        for name in (workspace_types.VERIFY_SKIPPED, workspace_types.VERIFY_DELETED):
            (directory / name).symlink_to(outside)
            self.assertIsNone(settings.ledger(project.root, name))
        self.assertEqual(worker.verify_skipped_path(project.root, {}), directory / workspace_types.VERIFY_SKIPPED)
        self.assertEqual(outside.read_text(), 'unchanged')
