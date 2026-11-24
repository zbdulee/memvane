"""A released manifest can protect a target without blocking other first reviews."""
import json

from memvane.wiki.compile.tests import legacy_created
from memvane.wiki.compile.tests.integration import test_pending_handoff as fixtures
from tests.support.case import FixtureCase


class MixedLedgerFirstReview(FixtureCase):
    prepare = fixtures.PendingHandoff.prepare
    write = fixtures.PendingHandoff.write
    native_verify = fixtures.PendingHandoff.native_verify

    def test_first_review_next_to_a_released_card(self):
        for separator in ('-', '\x85', '\u2028', '\u2029'):
            with self.subTest(separator=ascii(separator)):
                self.prepare('mixed-' + str(ord(separator)))
                self.target.unlink()
                original = legacy_created.old_created(self, separator)
                manifest = self.queue.with_name('generated-manifest.jsonl')
                inode = manifest.stat().st_ino
                self.assertEqual(self.write('Newest source for unrelated review.'), 0)
                before = manifest.read_bytes()
                self.assertTrue(before.startswith(original))
                self.assertEqual(self.native_verify(), {'calls': 1, 'remaining': 0})
                self.assertEqual(self.native_verify(), {'calls': 0, 'remaining': 0})
                self.assertIn('status: verified', self.target.read_text().splitlines())
                self.assertEqual((manifest.read_bytes(), manifest.stat().st_ino), (before, inode))

    def test_conflict_before_review_or_before_stamp_preserves_the_generated_card(self):
        for phase in ('start', 'stamp'):
            with self.subTest(phase=phase):
                self.prepare('conflict-' + phase)
                self.target.unlink()
                row = json.loads(legacy_created.old_created(self, '\u2028'))
                self.assertEqual(self.write('Newest source for unrelated review.'), 0)
                original = self.target.read_bytes()
                manifest = self.queue.with_name('generated-manifest.jsonl')
                if phase == 'start':
                    row['targetPath'] = self.target.relative_to(self.root).as_posix()
                else:
                    row['sources'].append({'path': self.source.relative_to(self.root).as_posix()})

                def append_conflict():
                    with manifest.open('ab') as stream:
                        stream.write((json.dumps(row, ensure_ascii=False) + '\n').encode())

                if phase == 'start':
                    append_conflict()
                during_model = append_conflict if phase == 'stamp' else None
                self.assertEqual(self.native_verify(during_model), {'calls': int(phase == 'stamp'), 'remaining': 1})
                self.assertEqual(self.native_verify(), {'calls': 0, 'remaining': 1})
                self.assertEqual(self.target.read_bytes(), original)
                self.assertIn('status: generated', self.target.read_text().splitlines())
