"""Resume the originally paid verification across the pre-exchange stamp boundary."""
from datetime import datetime, timezone
import errno
import json
from unittest.mock import patch

from memvane.wiki.verify import promotion, verification_stamp
from memvane.wiki.verify.tests import support
from memvane.wiki.verify.tests.integration import test_promotion as cases


class Clock(datetime):
    day = 1

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, cls.day, tzinfo=tz or timezone.utc)


class IncompleteStamp(support.VerifyCase):
    verify_one = cases.Promotion.verify_one

    def setUp(self):
        super().setUp()
        self.original = self.setup().encode()
        self.original_inode = self.card.stat().st_ino
        self.review_job = self.job()
        self.engine = self.fake()
        Clock.day = 1
        clock = patch.object(verification_stamp, 'datetime', Clock)
        clock.start()
        self.addCleanup(clock.stop)

    def interrupt(self):
        with patch.object(promotion, 'publish', side_effect=OSError(errno.ENOSPC, 'before intent')):
            self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.review, = self.compile_dir.glob('retired-wiki/.promotions/*/review.md')
        self.assertFalse(self.review.with_name('intent').exists())
        self.assertIn(b'status: verified', self.review.read_bytes())
        self.assertEqual(len(self.engine.calls), 1)
        Clock.day = 2

    def test_recovers_first_day_image_and_original_inode(self):
        self.interrupt()
        expected = self.review.read_bytes()
        for _ in range(2):
            self.assertEqual(self.verify_one(self.review_job), (True, False))
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.card.read_bytes(), expected)
        self.assertIn(b'2026-01-01T', expected)
        archive = json.loads(self.review.with_name('archive.json').read_bytes())
        displaced = self.root / archive['archivePath']
        self.assertEqual(displaced.read_bytes(), self.original)
        self.assertEqual(displaced.stat().st_ino, self.original_inode)
        self.assertEqual((self.root / archive['basePath']).read_bytes(), self.original)

    def test_edited_target_and_stale_source_are_never_exchanged(self):
        self.interrupt()
        memo = self.original + b'operator annotation\n'
        self.card.write_bytes(memo)
        self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.assertEqual(self.card.read_bytes(), memo)
        self.card.write_bytes(self.original)
        self.source.write_text('The source was edited independently.\n')
        self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.assertEqual(self.card.read_bytes(), self.original)
        self.assertFalse(self.review.with_name('intent').exists())
        self.assertEqual(len(self.engine.calls), 1)

    def test_same_bytes_saved_to_another_inode_before_decision_are_not_adopted(self):
        read = promotion._source_bytes

        def read_then_save(*args, **kwargs):
            result = read(*args, **kwargs)
            cases.atomic_save(self.card, self.original)
            return result

        with patch.object(promotion, '_source_bytes', side_effect=read_then_save):
            self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.assertNotEqual(self.card.stat().st_ino, self.original_inode)
        self.assertEqual(self.card.read_bytes(), self.original)
        self.assertEqual(list(self.compile_dir.rglob('stamp-decision')), [])

    def test_foreign_job_and_tampered_review_cannot_consume_the_decision(self):
        self.interrupt()
        foreign = dict(self.review_job, engine='codex')
        self.assertEqual(self.verify_one(foreign), (False, True))
        self.review.write_text('unrelated image')
        self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.assertFalse(self.review.with_name('intent').exists())
        self.assertEqual(self.card.read_bytes(), self.original)
        self.assertEqual(len(self.engine.calls), 1)

    def test_decision_survives_even_when_review_image_cannot_be_created(self):
        write = promotion.write_new_file

        def fail_review(path, *args, **kwargs):
            if path.name == 'review.md':
                raise OSError(errno.ENOSPC, 'before review')
            return write(path, *args, **kwargs)

        with patch.object(promotion, 'write_new_file', side_effect=fail_review):
            self.assertEqual(self.verify_one(self.review_job), (False, True))
        self.assertEqual(list(self.compile_dir.rglob('review.md')), [])
        Clock.day = 2
        self.assertEqual(self.verify_one(self.review_job), (True, False))
        self.assertIn('2026-01-01T', self.card_now())
        self.assertEqual(len(self.engine.calls), 1)
