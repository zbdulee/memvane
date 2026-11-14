"""Compile queue recovery retains work and bounds retries under the workspace API."""
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase


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

    def test_live_pid_older_than_backstop_is_recovered(self):
        orphan = self.orphan('old', {'path': 'docs/old.md'}, pid=os.getppid())
        old = time.time() - 25 * 3600
        os.utime(orphan, (old, old))
        claimed = workspace.claim_queue(self.queue)
        self.assertEqual(json.loads(claimed.read_text())['path'], 'docs/old.md')
        self.assertFalse(orphan.exists())

    def test_requeue_write_failure_keeps_original_claim(self):
        orphan = self.orphan('unwritable', {'path': 'docs/retry.md'})
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == self.queue and args and args[0] == 'a':
                raise OSError('synthetic queue write failure')
            return real_open(path, *args, **kwargs)
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(self.dead_pid)}), \
             patch.object(Path, 'open', guarded_open):
            self.assertIsNone(workspace.claim_queue(self.queue))
        self.assertTrue(orphan.exists())
        with patch.dict(os.environ, {'QMD_FORCE_DEAD_PIDS': str(self.dead_pid)}):
            claimed = workspace.claim_queue(self.queue)
        self.assertEqual(json.loads(claimed.read_text())['path'], 'docs/retry.md')
