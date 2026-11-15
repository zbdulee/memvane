"""Compile batching coalesces source edits without losing a needed wake."""
from datetime import datetime, timedelta, timezone
import unittest

from memvane.wiki.compile import wiki_compile_worker as worker


class WorkerBatchRules(unittest.TestCase):
    def test_recent_edit_waits_with_bounded_delay_but_flush_and_full_batch_open(self):
        now = datetime.now(timezone.utc)
        fresh = ('row', {'ts': now.isoformat(), 'source': {'path': 'docs/a.md', 'collection': 'raw'}})
        self.assertFalse(worker.batch_ready([fresh], 90, 5, False))
        self.assertGreaterEqual(worker.batch_wake_after_seconds([fresh], 90, 5, False), 1)
        self.assertLessEqual(worker.batch_wake_after_seconds([fresh], 90, 5, False), 90)
        self.assertTrue(worker.batch_ready([fresh], 90, 5, True))
        self.assertTrue(worker.batch_ready([fresh] * 5, 90, 5, False))
        older = ('old', {**fresh[1], 'ts': (now - timedelta(seconds=100)).isoformat()})
        self.assertTrue(worker.batch_ready([older], 90, 5, False))

    def test_same_source_across_cwd_aliases_coalesces_to_latest(self):
        old = {'ts': '2026-01-01T00:00:00Z', 'cwd': '/var/project',
               'source': {'path': 'docs/a.md', 'collection': 'raw'}}
        new = {**old, 'ts': '2026-01-01T00:00:01Z', 'cwd': '/private/var/project'}
        other = {**old, 'source': {'path': 'docs/b.md', 'collection': 'raw'}}
        kept, dropped = worker.dedup_jobs([('old', old), ('other', other), ('new', new)])
        self.assertEqual([raw for raw, _ in kept], ['new', 'other'])
        self.assertEqual(dropped, ['old'])
