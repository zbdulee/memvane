"""The native verifier consumes LF-framed jobs and preserves every uninterpretable byte."""
import errno
import json
from unittest.mock import patch

from memvane.wiki.verify.tests.support import VerifyCase


class NativeOccurrenceReview(VerifyCase):
    def test_verified_singleton_can_settle_with_no_private_control_but_a_changed_revision_cannot(self):
        from memvane.wiki.card import api as card
        from memvane.wiki.verify import completed_review
        from memvane.wiki.verify import wiki_verify_worker as worker
        self.setup()
        job = self.job()
        revisions = card.dump_source_revisions(job['sourceRevisions'])
        self.setup(extra_fm='sourceRevisions:\n' + '\n'.join('  - ' + row for row in revisions))
        engine = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assert_status('verified')
        published = self.card_now()
        original_source = self.source.read_bytes()
        raw = json.dumps(job).encode() + b'\r\n'
        self.queue.write_bytes(raw)
        full = OSError(errno.ENOSPC, 'no private write')
        claim = worker.claim_queue
        def private_filled(queue, **kwargs):
            if queue.exists() and queue.stat().st_size and kwargs.get('private'):
                raise full
            return claim(queue, **kwargs)
        with patch.object(worker, 'claim_queue', side_effect=private_filled), \
             patch.object(completed_review.Work, 'create', side_effect=full) as create:
            self.source.write_bytes(original_source + b'changed')
            blocked = self.run_verify()
            self.assertEqual((blocked['processed'], blocked['remaining']), (0, 1))
            self.assertEqual(self.queue.read_bytes(), raw)
            self.source.write_bytes(original_source)
            result = self.run_verify()
            self.assertEqual((result['processed'], result['remaining']), (1, 0))
            self.assertEqual(self.queue.read_bytes(), b'')
            self.assertGreaterEqual(create.call_count, 1)
            self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(self.card_now(), published)

    def test_private_fingerprint_and_a_public_log_field_have_separate_outcomes(self):
        engine = self.fake()
        for char in ('\u0085', '\u2028', '\u2029'):
            for escaped in (True, False):
                with self.subTest(char=ord(char), escaped=escaped):
                    self.setup()
                    base = self.job()
                    branch = 'side' + char + 'branch'
                    good = dict(base, sourceFingerprints={'capture': {'branchKey': branch}})
                    unsafe = dict(base, trigger=branch)
                    def line(job):
                        return json.dumps(job, ensure_ascii=escaped).encode() + b'\r\n'
                    opaque = b'{"cut":"\xc3\n'
                    self.queue.write_bytes(line(unsafe) + opaque + line(good))
                    before = len(engine.calls)
                    result = self.run_verify()
                    self.assertEqual(result['processed'], 1)
                    self.assertEqual(len(engine.calls) - before, 1)
                    self.assert_status('verified')
                    self.assertEqual(self.queue.read_bytes(), b'')
                    retained = [p.read_bytes() for p in self.compile_dir.rglob('fragment-*')]
                    self.assertIn(opaque, retained)
                    self.assertIn(line(unsafe), retained)
                    for audit in self.compile_dir.glob('*.jsonl'):
                        if audit != self.queue:
                            self.assertNotIn(branch.encode(), audit.read_bytes())
                            self.assertTrue(all(isinstance(json.loads(r), dict) for r in audit.read_bytes().splitlines()))
                    self.assertEqual(self.run_verify()['processed'], 0)

    def test_oversized_card_keeps_its_job_and_independent_neighbor_is_actually_verified(self):
        from memvane.store.types import MAX_BODY
        self.setup()
        job = self.job()
        big = self.card.with_name('large.md')
        normal = self.card.read_bytes()
        big.write_bytes(normal + b' ' * (MAX_BODY + 1 - len(normal)))
        large_job = dict(job, targetPath=big.relative_to(self.root).as_posix())
        self.write_queue(large_job, job)
        engine = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assert_status('verified')
        self.assertEqual(len(engine.calls), 1)
        for _ in range(2):
            self.assertEqual(self.run_verify()['processed'], 0)
        self.assertEqual(len(engine.calls), 1)
        self.assertIn(large_job['targetPath'], self.queued())
        big.write_bytes(normal)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertIn(b'status: verified', big.read_bytes())
        self.assertEqual(self.queued(), '')
        self.assertEqual(len(engine.calls), 2)
