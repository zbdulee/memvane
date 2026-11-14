"""Pending replacements: a recompiled card is published only after it verifies.

The compiler writes ``.<card>.pending-<sourceHash>`` beside a published card
and queues a job naming it; the live card stays until the candidate verifies.
Cleanup removes only pending files nothing references, by their recorded
fingerprint, and never pays a verifier twice for it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.wiki.verify import wiki_verify_worker as worker
from memvane.wiki.verify.tests.support import CARD_REL, FAIL, HASH, PASS, VerifyCase, card_text


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def pending_rel(source_hash):
    return '.memvane/wiki/concepts/.test-card.md.pending-' + source_hash


class PendingCase(VerifyCase):
    def stage(self, source_hash, *, candidate_hash=None, **job):
        """A verified live card and a generated candidate for ``source_hash``; returns the job."""
        self.old = self.setup(card_status='verified')
        candidate = self.root / pending_rel(source_hash)
        candidate.write_text(card_text('generated', candidate_hash or source_hash), encoding='utf-8')
        job = self.job(sourceHash=source_hash, pendingPath=pending_rel(source_hash),
                       expectedTargetHash=sha256(self.old), **job)
        self.write_queue(job)
        return job

    def pending(self, source_hash):
        return self.root / pending_rel(source_hash)

    def archived(self):
        directory = self.compile_dir / 'retired-wiki'
        return sorted(p for p in directory.iterdir() if p.suffix == '.md') if directory.exists() else []


class Publish(PendingCase):
    def test_a_candidate_is_published_only_after_it_verifies(self):
        revision = 'a' * 16
        self.stage(revision)
        self.assertEqual(self.card_now(), self.old)
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assert_status('verified')
        self.assertIn(revision, self.card_now())
        self.assertFalse(self.pending(revision).exists())
        archived = self.archived()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(encoding='utf-8'), self.old, 'the previous card stays recoverable')
        self.assertEqual(self.queued(), '')

    def test_a_failed_candidate_leaves_the_published_card_unchanged(self):
        for action in ('delete', 'none'):
            with self.subTest(onFail=action):
                revision = 'b' * 16
                self.stage(revision)
                self.fake(FAIL)
                self.assertEqual(self.run_verify(verify={'onFail': action})['processed'], 1)
                self.assertEqual(self.card_now(), self.old)
                self.assertFalse(self.pending(revision).exists(), 'only the candidate is removed')
                self.assertEqual(self.log()[-1]['result'], 'pending_deleted' if action == 'delete' else 'kept')
                self.assertTrue(self.log()[-1].get('pendingOnly') or action == 'none')

    def test_a_superseded_candidate_is_acknowledged_without_overwriting_the_newer_card(self):
        first, second = 'a' * 16, 'b' * 16
        job = self.stage(first)
        self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        newer = self.card_now()
        stale = {**job, 'sourceHash': second, 'pendingPath': pending_rel(second)}
        self.pending(second).write_text(card_text('generated', second), encoding='utf-8')
        self.write_queue(stale)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(self.card_now(), newer)
        self.assertFalse(self.pending(second).exists())
        self.assertEqual(self.queued(), '')
        self.assertEqual(self.log()[-1]['result'], 'stale_replacement')

    def test_a_crash_after_publish_replays_as_verified_without_a_call(self):
        revision = 'c' * 16
        self.stage(revision)
        self.fake()
        self.run_verify()
        published = self.card_now()
        # The queue ACK was lost: the same job comes back after the candidate was published.
        self.write_queue(self.job(sourceHash=revision, pendingPath=pending_rel(revision),
                                  expectedTargetHash=sha256(self.old)))
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.card_now(), published)
        self.assertEqual(self.log()[-1]['result'], 'verified_replay')

    def test_invalid_pending_jobs_are_refused_before_any_call(self):
        revision = '0123456789abcdef'
        invalid = [
            {'sourceHash': None}, {'sourceHash': ''}, {'sourceHash': 'a' * 15}, {'sourceHash': 'a' * 17},
            {'sourceHash': 'a' * 64}, {'sourceHash': revision.upper()}, {'sourceHash': 'g' * 16},
            {'expectedTargetHash': 'a' * 16}, {'expectedTargetHash': 'a' * 63}, {'expectedTargetHash': 'g' * 64},
            {'pendingPath': pending_rel('f' * 16)}, {'pendingPath': '../outside.md'},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                job = self.stage(revision)
                self.write_queue({**job, **overrides})
                fake = self.fake()
                self.assertEqual(self.run_verify()['processed'], 1)
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.card_now(), self.old)
                self.assertTrue(self.pending(revision).exists(), 'an invalid job cannot remove private bytes')
                row = self.log()[-1]
                self.assertEqual((row['result'], row['reason']), ('skipped', 'pending_replacement_invalid'))


class Cleanup(PendingCase):
    def test_superseded_candidates_are_cleaned_and_the_latest_is_verified(self):
        a, b = 'a' * 16, 'b' * 16
        job = self.stage(a)
        later = {**job, 'ts': '2026-07-05T00:00:00Z', 'sourceHash': b, 'pendingPath': pending_rel(b)}
        self.pending(b).write_text(card_text('generated', b), encoding='utf-8')
        self.write_queue(job, later)
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(self.pending(a).exists())
        self.assertFalse(self.pending(b).exists())
        self.assertIn(b, self.card_now())
        self.assertEqual(self.queued(), '')

    def test_a_candidate_referenced_by_another_active_claim_is_kept(self):
        a, b = 'a' * 16, 'b' * 16
        job = self.stage(a)
        later = {**job, 'ts': '2026-07-05T00:00:00Z', 'sourceHash': b, 'pendingPath': pending_rel(b)}
        self.pending(b).write_text(card_text('generated', b), encoding='utf-8')
        self.write_queue(job, later)
        other = self.queue.with_name(self.queue.name + '.claimed.%d.active' % os.getpid())
        other.write_text(json.dumps(job) + '\n', encoding='utf-8')
        self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertTrue(self.pending(a).exists())
        self.assertFalse(self.pending(b).exists())

    def test_a_candidate_edited_during_verification_is_preserved(self):
        revision = 'c' * 16
        self.stage(revision)

        def edit_then_pass(payload, argv):
            path = self.pending(revision)
            path.write_text(path.read_text(encoding='utf-8') + '\nuser edit\n', encoding='utf-8')
            return PASS
        self.fake(edit_then_pass)
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertIn('user edit', self.pending(revision).read_text(encoding='utf-8'))
        self.assertEqual(self.card_now(), self.old)
        # A later unrelated job does not sweep the edited candidate either.
        nxt = 'd' * 16
        self.root.joinpath(pending_rel(nxt)).write_text(card_text('generated', nxt), encoding='utf-8')
        self.write_queue({'ts': '2026-07-05T00:00:00Z', 'targetPath': CARD_REL, 'sourceHash': nxt,
                          'pendingPath': pending_rel(nxt), 'expectedTargetHash': sha256(self.old),
                          'sources': [], 'engine': 'claude'})
        self.run_verify()
        self.assertIn('user edit', self.pending(revision).read_text(encoding='utf-8'))

    def test_an_append_to_the_claimed_queue_stays_queued_with_its_candidate(self):
        a, b = 'a' * 16, 'b' * 16
        job = self.stage(a)
        late = {**job, 'ts': '2026-07-05T00:00:00Z', 'sourceHash': b, 'pendingPath': pending_rel(b)}
        real_read = worker.read_queue
        fired = []

        def read(path):
            rows = real_read(path)
            if '.claimed.' in path.name and not fired:
                fired.append(True)
                self.pending(b).write_text(card_text('generated', b), encoding='utf-8')
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(late) + '\n')
            return rows
        self.fake()
        with mock.patch.object(worker, 'read_queue', side_effect=read):
            self.run_verify()
        self.assertTrue(self.pending(b).exists())
        holders = [self.queue] + sorted(self.compile_dir.glob('verify-queue.jsonl.claimed.*'))
        self.assertTrue(any(pending_rel(b) in p.read_text(encoding='utf-8') for p in holders),
                        'the late append stays durable')

    def test_a_blocked_cleanup_retries_without_calling_the_verifier_again(self):
        revision = 'e' * 16
        job = self.stage(revision)
        self.write_queue(job, raw=['{malformed'])
        fake = self.fake(FAIL)
        self.assertEqual(self.run_verify(verify={'onFail': 'none'})['processed'], 1)
        self.assertTrue(self.pending(revision).exists())
        self.assertEqual(len(fake.calls), 1)
        repaired = [line for line in self.queued().splitlines() if line and line != '{malformed']
        self.queue.write_text(''.join(line + '\n' for line in repaired), encoding='utf-8')
        self.run_verify(verify={'onFail': 'none'})
        self.assertFalse(self.pending(revision).exists())
        self.assertEqual(len(fake.calls), 1)

    def test_a_transient_unlink_error_retries_cleanup_without_another_call(self):
        revision = 'f' * 16
        self.stage(revision)
        fake = self.fake(FAIL)
        target = self.pending(revision)
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == target:
                raise PermissionError('synthetic unlink failure')
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', unlink):
            self.run_verify(verify={'onFail': 'none'})
        self.assertTrue(target.exists())
        self.assertEqual(len(fake.calls), 1)
        self.run_verify(verify={'onFail': 'none'})
        self.assertFalse(target.exists())
        self.assertEqual(len(fake.calls), 1)

    def test_a_stale_candidate_is_cleaned_but_a_symlink_target_is_never_touched(self):
        revision = 'c' * 16
        job = self.stage(revision, candidate_hash='d' * 16)
        fake = self.fake()
        self.assertEqual(self.run_verify()['processed'], 1)
        self.assertFalse(self.pending(revision).exists())
        outside = self.root / 'outside.txt'
        outside.write_text('keep', encoding='utf-8')
        self.pending(revision).symlink_to(outside)
        self.write_queue(job)
        self.run_verify()
        self.assertEqual(outside.read_text(encoding='utf-8'), 'keep')
        self.assertTrue(self.pending(revision).is_symlink())
        self.assertEqual(fake.calls, [])


if __name__ == '__main__':
    unittest.main()
