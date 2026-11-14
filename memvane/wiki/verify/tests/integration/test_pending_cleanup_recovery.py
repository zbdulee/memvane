"""Recovering pending-candidate cleanup across crashes, late appends and conflicting receipts.

A cleanup receipt (``_pendingCleanup``) binds one exact job to the candidate
fingerprint seen before the crash; nothing without a matching receipt may
delete a candidate or skip its verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from memvane.wiki.verify import wiki_verify_worker as verify
from tests.support import settings_fake
from tests.support.case import FixtureCase
from tests.support.project import make_project


def fingerprint(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class PendingRecovery(FixtureCase):
    def setUp(self):
        super().setUp()
        project = make_project(self.home, git=False)
        self.root = project.root.resolve()
        (project.wiki / 'concepts').mkdir(parents=True)
        compile_dir = self.root / '.memvane' / 'compile'
        compile_dir.mkdir(parents=True)
        self.queue = compile_dir / 'verify-queue.jsonl'
        self.queue.write_text('', encoding='utf-8')
        self.target = project.wiki / 'concepts' / 'card.md'
        self.target.write_text('live', encoding='utf-8')
        self.pending = self.target.with_name('.card.md.pending-' + 'a' * 16)
        self.pending.write_text('candidate', encoding='utf-8')
        self.job = {'targetPath': self.target.relative_to(self.root).as_posix(),
                    'pendingPath': self.pending.relative_to(self.root).as_posix(),
                    'sourceHash': 'a' * 16, 'expectedTargetHash': 'b' * 64, 'ts': '2026-01-01T00:00:00Z'}
        self.cfg = settings_fake.expanded(project)
        self.compile = self.cfg['compile']

    def run_verify(self):
        return verify.run(self.root, self.cfg, self.compile)

    def claim(self, name, content=''):
        path = self.queue.with_name(self.queue.name + '.claimed.%d.%s' % (os.getpid(), name))
        path.write_text(content, encoding='utf-8')
        return path

    def receipt(self, job=None, print_=None):
        job = job or self.job
        return {**job, '_pendingCleanup': {'fingerprint': print_ or fingerprint(self.pending),
                                           'jobKey': verify._cleanup_job_key(job)}}

    def write_queue(self, *rows, raw=()):
        self.queue.write_text(''.join(json.dumps(row) + '\n' for row in rows) + ''.join(r + '\n' for r in raw),
                              encoding='utf-8')

    def queued_rows(self):
        rows = []
        for line in self.queue.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    def test_a_late_append_to_an_empty_claim_is_not_lost(self):
        claim = self.claim('empty')
        raw = json.dumps(self.job)

        def read(path):
            self.assertEqual(path, claim)
            claim.write_text(raw + '\n', encoding='utf-8')
            return []
        with mock.patch.object(verify, 'claim_queue', return_value=claim), \
                mock.patch.object(verify, 'read_queue', side_effect=read):
            self.run_verify()
        self.assertTrue(self.pending.exists())
        self.assertTrue(raw in self.queue.read_text(encoding='utf-8')
                        or (claim.exists() and raw in claim.read_text(encoding='utf-8')))

    def test_an_unreadable_claim_is_not_unlinked(self):
        claim = self.claim('unreadable', json.dumps(self.job) + '\n')
        real = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == claim:
                raise PermissionError('synthetic')
            return real(path, *args, **kwargs)
        with mock.patch.object(verify, 'claim_queue', return_value=claim), \
                mock.patch.object(verify, 'read_queue', return_value=[]), \
                mock.patch.object(Path, 'read_text', read_text):
            self.run_verify()
        self.assertTrue(claim.exists())

    def test_a_recovered_old_job_cannot_override_a_saved_fingerprint(self):
        saved = fingerprint(self.pending)
        self.pending.write_text('candidate with user edits', encoding='utf-8')
        retry = self.receipt(print_=saved)
        recovered = {**self.job, '_requeue_count': 1}
        snapshots, _ = verify._pending_snapshots(self.root, self.cfg,
                                                 [(json.dumps(retry), retry), (json.dumps(recovered), recovered)])
        self.assertEqual(snapshots[self.pending], tuple(saved))

    def test_a_recovered_job_does_not_reverify_an_edited_cleanup_candidate(self):
        saved = fingerprint(self.pending)
        self.pending.write_text('candidate with user edits', encoding='utf-8')
        self.write_queue(self.receipt(print_=saved), {**self.job, '_requeue_count': 1})
        with mock.patch.object(verify, 'process_verify_job', side_effect=AssertionError('must not reverify')):
            self.run_verify()
        self.assertEqual(self.pending.read_text(encoding='utf-8'), 'candidate with user edits')

    def test_conflicting_cleanup_fingerprints_do_not_authorize_deletion(self):
        earlier = self.receipt()
        self.pending.write_text('candidate with user edits', encoding='utf-8')
        later = self.receipt()
        snapshots, _ = verify._pending_snapshots(self.root, self.cfg,
                                                 [(json.dumps(earlier), earlier), (json.dumps(later), later)])
        self.assertNotIn(self.pending, snapshots)
        self.write_queue(earlier, later, {**self.job, '_requeue_count': 1})
        with mock.patch.object(verify, 'process_verify_job', side_effect=AssertionError('must not reverify')):
            self.run_verify()
        self.assertEqual(self.pending.read_text(encoding='utf-8'), 'candidate with user edits')

    def test_an_unprocessed_deferred_job_never_gets_a_cleanup_receipt(self):
        other = self.target.with_name('other.md')
        other.write_text('live', encoding='utf-8')
        next_pending = other.with_name('.other.md.pending-' + 'b' * 16)
        next_pending.write_text('candidate', encoding='utf-8')
        later = {**self.job, 'targetPath': other.relative_to(self.root).as_posix(),
                 'pendingPath': next_pending.relative_to(self.root).as_posix(), 'sourceHash': 'b' * 16}
        self.write_queue(self.job, later, raw=['{malformed'])
        self.compile = {**self.compile, 'budget': {'verifyPerRun': 1}}
        with mock.patch.object(verify, 'process_verify_job', return_value=(True, False)):
            self.run_verify()
        rows = self.queued_rows()
        self.assertTrue(any(r.get('pendingPath') == later['pendingPath'] and '_pendingCleanup' not in r
                            for r in rows))
        self.assertFalse(any(r.get('pendingPath') == later['pendingPath'] and '_pendingCleanup' in r
                             for r in rows))
        self.assertTrue(next_pending.exists())

    def test_a_malformed_or_null_cleanup_marker_cannot_suppress_or_delete(self):
        self.write_queue({**self.job, '_pendingCleanup': {}}, {**self.job, '_requeue_count': 1})
        with mock.patch.object(verify, 'process_verify_job', return_value=(False, True)) as call:
            self.run_verify()
        self.assertEqual(call.call_count, 1)
        self.assertTrue(self.pending.exists())
        self.write_queue({**self.job, '_pendingCleanup': None})
        with mock.patch.object(verify, 'process_verify_job', side_effect=AssertionError('invalid must not verify')):
            self.run_verify()
        self.assertTrue(self.pending.exists())

    def test_a_receipt_cannot_suppress_a_different_recovered_job(self):
        other = {**self.job, 'ts': '2026-01-02T00:00:00Z', '_requeue_count': 1}
        self.write_queue(self.receipt(), other)
        with mock.patch.object(verify, 'process_verify_job', return_value=(False, True)) as call:
            self.run_verify()
        self.assertEqual(call.call_count, 1)
        self.assertTrue(self.pending.exists())

    def test_an_existing_receipt_survives_a_budget_deferred_recovered_job(self):
        retry = self.receipt()
        other = self.target.with_name('other.md')
        other.write_text('live', encoding='utf-8')
        first = {**self.job, 'targetPath': other.relative_to(self.root).as_posix(),
                 'pendingPath': other.with_name('.other.md.pending-' + 'b' * 16).relative_to(self.root).as_posix(),
                 'sourceHash': 'b' * 16}
        (self.root / first['pendingPath']).write_text('candidate', encoding='utf-8')
        self.write_queue(first, retry, {**self.job, '_requeue_count': 1})
        self.compile = {**self.compile, 'budget': {'verifyPerRun': 1}}
        with mock.patch.object(verify, 'process_verify_job', return_value=(True, False)):
            self.run_verify()
        self.assertTrue(any(r.get('pendingPath') == retry['pendingPath'] and '_pendingCleanup' in r
                            for r in self.queued_rows()))
        self.assertTrue(self.pending.exists())

    def test_two_cleanup_claims_do_not_both_ack_a_shared_candidate(self):
        row = json.dumps(self.job)
        a, b = self.claim('a', row + '\n'), self.claim('b', row + '\n')
        snapshots, _ = verify._pending_snapshots(self.root, self.cfg, [(row, self.job)])
        self.assertIs(verify._cleanup_pending(self.root, self.queue, a, snapshots, [(row, self.job)]), False)
        self.assertIs(verify._cleanup_pending(self.root, self.queue, b, snapshots, [(row, self.job)]), False)
        self.assertTrue(self.pending.exists())

    def test_the_receipt_key_binds_the_exact_job(self):
        key = verify._cleanup_job_key(self.job)
        self.assertEqual(key, verify._cleanup_job_key({**self.job, '_requeue_count': 2}))
        self.assertNotEqual(key, verify._cleanup_job_key({**self.job, 'ts': '2026-01-02T00:00:00Z'}))
        self.assertTrue(verify._valid_cleanup_marker(self.receipt()))
        for marker in ({}, None, {'fingerprint': [1, 2, 3], 'jobKey': key},
                       {'fingerprint': [1, 2, 3, 4, -5], 'jobKey': key},
                       {'fingerprint': fingerprint(self.pending), 'jobKey': 'x' * 64},
                       {'fingerprint': fingerprint(self.pending), 'jobKey': key, 'extra': 1}):
            with self.subTest(marker=marker):
                self.assertFalse(verify._valid_cleanup_marker({**self.job, '_pendingCleanup': marker}))


if __name__ == '__main__':
    unittest.main()
