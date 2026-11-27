"""Rendering a verification stamp is deterministic and does not mutate its input."""
from datetime import datetime, timezone

from memvane.wiki.verify import verification_stamp
from tests.support.case import FixtureCase


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

