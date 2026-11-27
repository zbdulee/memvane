"""Replacement rows: the visible card changes only when the reviewed image is published.

Compile queues a ``replace`` row whose ``pending_blob`` is the new image and
whose ``expectedTargetHash`` is the visible card it replaces; no file is
written beside the card. These cases count verifier calls and read the rows.
"""
from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock

from memvane.wiki.card import api as cards
from memvane.wiki.verify import api as verify
from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import FAIL, INCONCLUSIVE, PASS, SOURCE, SOURCE_REL, VerifyCase, card_text

NEW_HASH = 'def456'


class ReplaceRows(VerifyCase):
    def stage(self, **overrides):
        """A visible verified card and a queued replacement for the current source revision."""
        self.project.write_note(SOURCE_REL, SOURCE)
        job = self.job(kind='replace', sourceHash=NEW_HASH, **overrides)
        revisions = cards.dump_source_revisions(job['sourceRevisions'])
        frontmatter = 'sourceRevisions:\n' + '\n'.join('  - ' + entry for entry in revisions)
        self.card.parent.mkdir(parents=True, exist_ok=True)
        self.card.write_text(card_text('verified').replace('cite markdown', 'old claim'), encoding='utf-8')
        self.old = self.card.read_bytes()
        job['expectedTargetHash'] = hashlib.sha256(self.old).hexdigest()
        self.image = card_text(source_hash=NEW_HASH, extra_fm=frontmatter)
        self.write_queue(job, images=[self.image.encode()])
        return job

    def hidden(self):
        return sorted(p.name for p in self.card.parent.iterdir() if p.name.startswith('.'))

    def test_a_passing_replacement_is_published_only_after_its_review(self):
        self.stage()
        seen = []

        def capture(payload, argv):
            seen.append(payload['card']['content'])
            return PASS
        fake = self.fake(capture)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(seen, [self.image], 'the image is what the verifier sees')
        self.assert_status('verified')
        self.assertIn(f'sourceHash="{NEW_HASH}"', self.card_now())
        row = self.rows()[-1]
        self.assertEqual((row['state'], row['reason'], row['has_blob']), ('done', 'verified', 0))
        self.assertEqual(self.hidden(), [])

    def test_a_negative_replacement_keeps_the_visible_card_and_drops_the_image(self):
        for answer in (FAIL, INCONCLUSIVE):
            with self.subTest(verdict=answer['verdict']):
                self.stage()
                fake = self.fake(answer)
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(self.card.read_bytes(), self.old)
                row = self.rows()[-1]
                self.assertEqual((row['state'], row['reason'], row['has_blob']), ('done', 'kept', 0))
                self.assertEqual(self.hidden(), [])
                self.assertEqual(self.run_verify()['processed'], 0)
                self.assertEqual(len(fake.calls), 1)

    def test_a_card_edited_before_the_claim_closes_the_row_without_a_call(self):
        self.stage()
        self.card.write_text('a person rewrote this card\n', encoding='utf-8')
        fake = self.fake(PASS)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.card_now(), 'a person rewrote this card\n')
        self.assertEqual(self.rows()[-1]['reason'], 'changed_before_verify')

    def test_a_card_edited_during_the_call_keeps_the_edit(self):
        self.stage()

        def edit_then_pass(payload, argv):
            self.card.write_text('saved during review\n', encoding='utf-8')
            return PASS
        fake = self.fake(edit_then_pass)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(self.card_now(), 'saved during review\n')
        self.assertEqual(self.rows()[-1]['reason'], 'changed_during_verify')
        self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(fake.calls), 1)

    def test_an_image_that_does_not_match_its_row_is_never_sent(self):
        job = self.stage()
        for image in (b'\xff\xfe', card_text(source_hash='other').encode(), None):
            with self.subTest(image=image):
                self.write_queue(job, images=[image])
                fake = self.fake(PASS)
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.rows()[-1]['reason'], 'replacement_invalid')
                self.assertEqual(self.card.read_bytes(), self.old)

    def test_a_run_ending_after_the_publish_settles_without_another_call_or_write(self):
        from memvane.wiki.pipeline import api as pipeline
        self.stage()
        fake = self.fake(PASS)

        from tests.support.wiki_handoff import failing_ledger_method
        with failing_ledger_method('finish_verify_job', RuntimeError('killed after the publish')), \
                mock.patch.object(worker.os, 'getpid', return_value=999999):
            with self.assertRaises(RuntimeError):
                self.run_verify()
        published = self.card.read_bytes()
        self.assertIn(b'status: verified', published)
        with pipeline.open_pipeline(self.root) as store:  # the killed run never released its claim
            store._conn.execute("UPDATE verify_jobs SET state='claimed', claim_pid=999999")
            store._conn.commit()
        with mock.patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '999999'}):
            self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.card.read_bytes(), published)
        self.assertEqual((self.rows()[-1]['reason'], self.log()[-1]['result']), ('verified', 'verified_replay'))


class CompileReads(VerifyCase):
    def revision(self):
        self.project.write_note(SOURCE_REL, SOURCE)
        return self.project.revision(self.source)

    def test_a_queued_replacement_for_this_revision_stops_a_second_extraction(self):
        stage = ReplaceRows.stage
        job = stage(self)
        config = self.config()
        revision = job['sourceRevisions'][0]
        self.assertEqual(verify.pending_source_review(self.root, config, revision), (job['targetPath'], False))
        # Another revision, or a visible card that changed, needs a new extraction.
        other = {**revision, 'sha256': '0' * 64}
        self.assertEqual(verify.pending_source_review(self.root, config, other), (None, False))
        self.card.write_text('edited\n', encoding='utf-8')
        self.assertEqual(verify.pending_source_review(self.root, config, revision), (None, False))

    def test_a_live_claim_citing_the_source_keeps_the_compile_job(self):
        from memvane.wiki.pipeline import api as pipeline
        job = ReplaceRows.stage(self)
        with pipeline.open_pipeline(self.root) as store:
            store.claim_verify_batch(os.getpid(), 'live', budget=1)
        self.assertEqual(verify.pending_source_review(self.root, self.config(), job['sourceRevisions'][0],
                                                      reuse=False), (None, True))

    def test_a_first_card_is_published_after_its_review_row(self):
        from memvane.wiki.pipeline import api as pipeline
        revision = self.revision()
        job = self.job(kind='review', sourceRevisions=[revision])
        image = card_text().encode()
        job['expectedTargetHash'] = hashlib.sha256(image).hexdigest()
        wiki = self.card.parents[1]
        wiki.mkdir(parents=True, exist_ok=True)
        self.assertTrue(verify.publish_first_card(self.root, wiki, job, image, review=True,
                                                  manifest={'targetPath': job['targetPath']}))
        self.assertEqual(self.card.read_bytes(), image)
        self.assertEqual([r['payload']['kind'] for r in self.rows()], ['review'])
        # A person's file at the path is never replaced; the row closes without a call.
        self.card.write_text('mine\n', encoding='utf-8')
        with pipeline.open_pipeline(self.root) as store:
            store._conn.execute('DELETE FROM verify_jobs')
            store._conn.commit()
        self.assertFalse(verify.publish_first_card(self.root, wiki, job, image, review=True,
                                                   manifest={'targetPath': job['targetPath']}))
        self.assertEqual(self.card_now(), 'mine\n')
        fake = self.fake(PASS)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual((fake.calls, self.rows()[-1]['reason']), ([], 'changed_before_verify'))


if __name__ == '__main__':
    unittest.main()
