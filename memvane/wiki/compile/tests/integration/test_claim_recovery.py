"""Compile queue recovery retains work and bounds retries under the workspace API."""
import json
import fcntl
import os
from pathlib import Path
import threading
import time
import uuid
from unittest.mock import patch

from memvane.wiki.workspace import api as workspace
from memvane.wiki.intake import api as intake
from tests.support.case import FixtureCase
from tests.support.queue_files import pending_text


class ClaimRecovery(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = self.tmp / 'compile/source-queue.jsonl'
        self.queue.parent.mkdir()
        self.dead_pid = 2147483647

    def orphan(self, suffix, value, pid=None):
        path = self.queue.with_name(self.queue.name + '.claimed.%d.%s' % (pid or self.dead_pid, suffix))
        path.write_text(json.dumps(value) + '\n')
        return path

    def test_orphan_and_corrupt_counter_are_requeued_without_losing_rows(self):
        first = self.orphan('first', {'path': 'docs/first.md'})
        second = self.orphan('corrupt', {'path': 'docs/second.md', '_requeue_count': 'invalid'})
        future = time.time() + 3600
        os.utime(first, (future, future))
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(self.dead_pid)}):
            claimed = workspace.claim_queue(self.queue)
        rows = [json.loads(line) for line in claimed.read_text().splitlines()]
        self.assertEqual({row['path'] for row in rows}, {'docs/first.md', 'docs/second.md'})
        self.assertTrue(all(row['_requeue_count'] == 1 for row in rows))
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertFalse(self.queue.read_text().strip())

    def test_live_owner_is_not_stolen_even_when_backlog_is_old(self):
        live = self.orphan('live', {'path': 'docs/live.md'}, pid=os.getppid())
        old = time.time() - 7200
        os.utime(live, (old, old))
        self.queue.write_text(json.dumps({'path': 'docs/waiting.md'}) + '\n')
        claimed = workspace.claim_queue(self.queue)
        self.assertEqual(json.loads(claimed.read_text())['path'], 'docs/waiting.md')
        self.assertTrue(live.exists())

    def test_poison_job_records_discard_and_backfill_receipt(self):
        orphan = self.orphan('poison', {'targetPath': 'docs/poison.md', '_requeue_count': 3})
        seen = []
        def receipt(job):
            seen.append(job['targetPath'])
            return {'fixture': 'receipt'}
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(self.dead_pid)}):
            self.assertIsNone(workspace.claim_queue(self.queue, receipt_writer=receipt))
        row = json.loads((self.queue.parent / 'discard-ledger.jsonl').read_text())
        self.assertEqual((row['targetPath'], row['reason'], row['requeue_count']),
                         ('docs/poison.md', 'max_requeue_exceeded', 4))
        self.assertEqual((seen, row['backfillReceipt']), (['docs/poison.md'], {'fixture': 'receipt'}))
        self.assertFalse(orphan.exists())

    def test_live_pid_older_than_backstop_keeps_its_claim(self):
        orphan = self.orphan('old', {'path': 'docs/old.md'}, pid=os.getppid())
        old = time.time() - 25 * 3600
        os.utime(orphan, (old, old))
        self.assertIsNone(workspace.claim_queue(self.queue))
        self.assertTrue(orphan.exists())

    def test_requeue_write_failure_keeps_original_claim(self):
        orphan = self.orphan('unwritable', {'path': 'docs/retry.md'})
        with patch.object(os, 'fsync', side_effect=OSError('synthetic ENOSPC')):
            with self.assertRaisesRegex(OSError, 'ENOSPC'):
                workspace.claim_queue(self.queue)
        self.assertTrue(orphan.exists())
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(self.dead_pid)}):
            claimed = workspace.claim_queue(self.queue)
        self.assertEqual(json.loads(claimed.read_text())['path'], 'docs/retry.md')

    def test_unreadable_orphan_does_not_block_a_separate_backlog(self):
        orphan = self.orphan('unreadable', {'path': 'docs/orphan.md'})
        self.queue.write_text(json.dumps({'path': 'docs/backlog.md'}) + '\n')
        real_read = os.open
        def guarded_read(path, *args, **kwargs):
            if path == orphan:
                raise OSError('synthetic read failure')
            return real_read(path, *args, **kwargs)
        with patch.object(os, 'open', side_effect=guarded_read):
            claimed = workspace.claim_queue(self.queue)
        self.assertTrue(orphan.exists())
        self.assertEqual(json.loads(claimed.read_text())['path'], 'docs/backlog.md')

    def test_discard_ledger_extracts_targets_from_each_queue_shape(self):
        cases = [('merge-needed.jsonl', {'candidate': {'targetPath': 'docs/candidate.md'}}, 'docs/candidate.md'),
                 ('dedup-needed.jsonl', {'pageA': 'a.md', 'pageB': 'b.md'}, 'a.md <-> b.md')]
        for name, job, _ in cases:
            self.queue = self.queue.parent / name
            self.orphan('poison', {**job, '_requeue_count': 3})
            self.assertIsNone(workspace.claim_queue(self.queue))
        ledger = self.queue.parent / 'discard-ledger.jsonl'
        self.assertEqual([json.loads(line)['targetPath'] for line in ledger.read_text().splitlines()],
                         [expected for _, _, expected in cases])

    def test_fourth_source_and_verify_orphan_are_native_until_completed(self):
        for name in ('source-queue.jsonl', 'verify-queue.jsonl'):
            with self.subTest(queue=name):
                self.queue = self.queue.parent / name
                row = {'path': 'a', 'forceRecompile': True, '_requeue_count': 3}
                orphan = self.orphan('four', row)
                original = orphan.read_bytes()
                claim = workspace.claim_queue(self.queue)
                self.assertEqual(claim.read_bytes(), original)
                self.assertNotEqual(claim.parent, self.queue.parent)
                self.assertFalse(orphan.exists())
                self.assertEqual(self.queue.read_bytes(), b'')
                self.assertTrue(workspace.queue_busy(self.queue))
                rows = workspace.read_queue(claim)
                rows.finish(self.queue, claim, [rows[0][0]])
                next_claim = workspace.claim_queue(self.queue)
                self.assertEqual(next_claim.read_bytes(), original)
                self.assertEqual(next_claim.stat().st_mode & 0o777, 0o600)
                workspace.read_queue(next_claim).finish(self.queue, next_claim, [])
                self.assertFalse(workspace.queue_busy(self.queue))
                self.assertFalse((self.queue.parent / 'discard-ledger.jsonl').exists())

    def test_return_separates_late_identical_occurrence_and_retains_torn_tail(self):
        row = b'{"forceRecompile":true}\n'
        self.queue.write_bytes(row)
        with self.queue.open('ab', buffering=0) as legacy:
            claim = workspace.claim_queue(self.queue)
            rows = workspace.read_queue(claim)
            self.assertEqual(rows.line_ranges, [(0, len(row))])
            legacy.write(row + b'{"partial":')
        rows.finish(self.queue, claim, [rows[0][0]])
        self.assertEqual(self.queue.read_bytes(), row * 2)
        self.assertEqual([p.read_bytes() for p in self.queue.parent.rglob('fragment-*')], [b'{"partial":'])

    def test_public_and_private_claim_name_collisions_keep_both_files(self):
        nonce = uuid.UUID('1' * 32)
        with patch.object(uuid, 'uuid4', return_value=nonce):
            self.queue.write_bytes(b'{"one":1}\n')
            first = workspace.claim_queue(self.queue)
            rows = workspace.read_queue(first)
            self.queue.write_bytes(b'{"two":2}\n')
            with self.assertRaises(FileExistsError):
                workspace.claim_queue(self.queue)
            self.assertEqual(first.read_bytes(), b'{"one":1}\n')
            self.assertEqual(self.queue.read_bytes(), b'{"two":2}\n')
            private = rows.privatize(self.queue, first)
            other = workspace.claim_queue(self.queue)
            with self.assertRaises(FileExistsError):
                workspace.read_queue(other).privatize(self.queue, other)
            self.assertEqual(private.read_bytes(), b'{"one":1}\n')
            self.assertEqual(other.read_bytes(), b'{"two":2}\n')
            ready = private.parent / ('ready-' + nonce.hex)
            ready.write_bytes(b'{"three":3}\n')
            with self.assertRaises(FileExistsError):
                workspace.claim_queue(self.queue)
            self.assertEqual(ready.read_bytes(), b'{"three":3}\n')
            self.assertEqual(private.read_bytes(), b'{"one":1}\n')

    def test_changed_prefix_keeps_original_claim_and_live(self):
        self.queue.write_bytes(b'{"a":1}\n')
        claim = workspace.claim_queue(self.queue)
        rows = workspace.read_queue(claim)
        claim.write_bytes(b'{"a":2}\n')
        with self.assertRaisesRegex(OSError, 'prefix changed'):
            rows.finish(self.queue, claim, [])
        self.assertTrue(claim.exists())
        self.assertEqual(self.queue.read_bytes(), b'')

    def test_consumed_swap_survives_released_owner_taking_live(self):
        row = b'{"forceRecompile":true}\n'
        self.queue.write_bytes(row)
        claim = workspace.claim_queue(self.queue)
        rows = workspace.read_queue(claim)
        real_replace = os.replace
        def interrupt(src, dst):
            real_replace(src, dst)
            if dst == self.queue:
                raise OSError('crash before recording publication')
        with patch.object(os, 'replace', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'publication'):
                rows.finish(self.queue, claim, [rows[0][0]])
        consumed = self.queue.with_name(self.queue.name + '.claimed.%d.old' % os.getpid())
        self.queue.rename(consumed)
        self.queue.write_bytes(b'')
        self.assertTrue(workspace.queue_busy(self.queue))  # released claim remains active
        self.assertEqual(consumed.read_bytes(), row)
        self.assertEqual(self.queue.read_bytes(), b'')
        consumed.unlink()
        empty = workspace.claim_queue(self.queue)
        rows = workspace.read_queue(empty)
        self.assertEqual(list(rows), [])
        rows.finish(self.queue, empty, [])
        self.assertFalse(workspace.queue_busy(self.queue))

    def test_orphan_with_unproved_legacy_return_prefix_keeps_both_occurrences(self):
        row = {'forceRecompile': True, 'source': {'path': 'both.md'}}
        orphan = self.orphan('legacy', row)
        with orphan.open('a') as handle:
            handle.write(json.dumps({'source': {'path': 'tail.md'}}) + '\n')
        self.queue.write_bytes((json.dumps(row) + '\n').encode() + b'{"source":')
        claim = workspace.claim_queue(self.queue)
        paths = [job['source']['path'] for _, job in workspace.read_queue(claim)]
        self.assertEqual(paths, ['both.md', 'both.md', 'tail.md'])
        self.assertEqual([p.read_bytes() for p in self.queue.parent.rglob('fragment-*')], [b'{"source":'])

    def test_each_return_fsync_and_rename_cut_preserves_one_occurrence(self):
        row = {'source': {'path': 'one.md'}, 'forceRecompile': True}
        def exercise(name, operation, stop, after):
            directory = self.tmp / name
            directory.mkdir()
            live = directory / 'source-queue.jsonl'
            claim = directory / (live.name + '.claimed.2147483647.test')
            claim.write_text(json.dumps(row) + '\n')
            rows = workspace.read_queue(claim)
            actual = getattr(os, operation)
            calls = []

            def interrupted(*args, **kwargs):
                calls.append(1)
                if len(calls) == stop and not after:
                    raise OSError('cut')
                result = actual(*args, **kwargs)
                if len(calls) == stop:
                    raise OSError('cut')
                return result

            with patch.object(os, operation, side_effect=interrupted):
                if stop is None:
                    rows.finish(live, claim, [rows[0][0]])
                else:
                    with self.assertRaisesRegex(OSError, 'cut'):
                        rows.finish(live, claim, [rows[0][0]])
            with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': '2147483647'}):
                recovered = workspace.claim_queue(live)
            result = workspace.read_queue(recovered)
            self.assertEqual([job['source']['path'] for _, job in result], ['one.md'])
            self.assertTrue(result[0][1]['forceRecompile'])
            result.finish(live, recovered, [])
            self.assertFalse(workspace.queue_busy(live))
            return len(calls)

        for operation in ('fsync', 'replace'):
            count = exercise('count-' + operation, operation, None, False)
            for stop in range(1, count + 1):
                for after in (False, True):
                    with self.subTest(operation=operation, stop=stop, after=after):
                        exercise('%s-%d-%d' % (operation, stop, after), operation, stop, after)

    def test_append_and_claim_share_lock_and_preserve_both_jobs(self):
        first = {'source': {'path': 'docs/a.md', 'collection': 'raw'}}
        second = {'source': {'path': 'docs/b.md', 'collection': 'raw'}}
        self.queue.write_text(json.dumps(first) + '\n')
        lock = workspace.queue_lock_path(self.queue)
        started, finished = threading.Event(), threading.Event()
        append_started, append_finished = threading.Event(), threading.Event()
        outcomes, errors = [], []
        def claim():
            started.set()
            try:
                outcomes.append(workspace.claim_queue(self.queue))
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        def append():
            append_started.set()
            try:
                intake.append_source_jobs(self.queue, [second])
            except Exception as exc:
                errors.append(exc)
            finally:
                append_finished.set()
        with lock.open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            thread = threading.Thread(target=claim)
            appender = threading.Thread(target=append)
            thread.start()
            appender.start()
            self.assertTrue(started.wait(2))
            self.assertTrue(append_started.wait(2))
            self.assertFalse(finished.wait(0.05), 'claim must wait for the shared queue lock')
            self.assertTrue(append_finished.wait(2), 'contended producer must defer without changing live')
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        thread.join(timeout=5)
        appender.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(appender.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BlockingIOError)
        intake.append_source_jobs(self.queue, [second])
        claimed = outcomes[0]
        self.assertTrue(claimed.is_file())
        rows = [json.loads(line) for line in pending_text(self.queue).splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['source']['path'] for row in rows}, {'docs/a.md', 'docs/b.md'})
