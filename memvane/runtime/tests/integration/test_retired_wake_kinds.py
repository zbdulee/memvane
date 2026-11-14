"""Verify F10/F11: the retired wake kinds are gone and an old row of one is dropped with a reason."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import runtime_work

TARGET = '/synthetic/project'


class RetiredKinds(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvk-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.root.chmod(0o700)
        self.queue = runtime_work.WakeQueue(self.root)

    def old_row(self, kind, target, *, status='pending', overflow=False):
        """A row as an older build wrote it, bypassing this build's validation."""
        with self.queue.connect() as db:
            key = policy.wake_key(kind, target)
            if overflow:
                db.execute('INSERT INTO wake_overflow VALUES (?,?,?,?)', (key, kind, target, 0))
            else:
                db.execute("INSERT INTO wakes VALUES (?,?,?,?,?,0,0,NULL)", (key, kind, target, 99, status))

    def drops(self):
        try:
            rows = json.loads((self.root / runtime_work.DROP_DETAIL_FILE).read_text())
        except FileNotFoundError:
            return {}
        return {entry['kind']: entry['reason'] for entry in rows.values()}

    def drain(self, execute, wall=10, max_runs=8):
        return runtime_work.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                                  wall=lambda: wall, sleep=lambda _s: None, max_runs=max_runs)

    def test_policy_knows_only_the_served_kinds(self):
        self.assertEqual(policy.KINDS, {'laya-sync': 2, 'laya-compile': 2, 'learning': 3, 'auto': 3})
        for kind, target in (('index', ''), ('update', 'a' * 32), ('cleanup', TARGET)):
            with self.assertRaises(ValueError) as caught:
                policy.validate_wake(kind, target)
            self.assertEqual(str(caught.exception), 'invalid_runtime_wake')
            with self.assertRaises(ValueError):
                self.queue.offer(kind, target, now=0)
        rows = [{'kind': 'learning', 'due': 0, 'id': 'a'}, {'kind': 'index', 'due': 0, 'id': 'b'},
                {'kind': 'laya-compile', 'due': 9, 'id': 'c'}, {'kind': 'laya-sync', 'due': 1, 'id': 'd'}]
        self.assertEqual(policy.due_rows(rows, 5), [rows[0], rows[3]])
        self.assertEqual(policy.next_row(policy.due_rows(rows, 9))['id'], 'd')
        self.assertFalse(policy.any_due([rows[1]], 5))
        self.assertEqual({policy.capacity_limit(kind) for kind in policy.KINDS}, {policy.MAX_WAKES - 1})

    def test_old_rows_of_retired_kinds_are_dropped_with_a_reason(self):
        self.old_row('index', '')
        self.old_row('update', 'a' * 32, status='running')
        self.old_row('cleanup', TARGET, overflow=True)
        self.old_row('future-kind', TARGET)  # a newer 1.x host's kind stays for that host
        self.queue.offer('laya-sync', TARGET, now=0)
        for _ in range(2):
            kinds = sorted(row['kind'] for row in self.queue.pending())
            self.assertEqual(kinds, ['future-kind', 'laya-sync'])
        self.assertEqual(self.drops(), {'index': 'retired_kind', 'update': 'retired_kind'})
        with self.queue.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wake_overflow').fetchone()[0], 0)
        ran = []
        self.assertEqual(self.drain(lambda row: ran.append(row['kind']) or True),
                         {'status': 'pending', 'runs': 1})
        self.assertEqual(ran, ['laya-sync'])
        self.assertEqual([row['kind'] for row in self.queue.pending()], ['future-kind'])

    def test_a_queue_of_only_retired_rows_starts_no_worker(self):
        self.old_row('index', '')
        with mock.patch.object(runtime_work.subprocess, 'Popen') as spawned, \
             mock.patch.object(runtime_work, '_arm_timer') as armed:
            self.assertEqual(runtime_work._wake(self.queue), 'idle')
        self.assertEqual((spawned.call_count, armed.call_count), (0, 0))
        self.assertEqual(self.queue.pending(), [])
        self.assertEqual(self.drops(), {'index': 'retired_kind'})

    def test_laya_kinds_dispatch_without_learning_admission_and_before_learning(self):
        self.queue.offer('learning', TARGET, now=0)
        self.queue.offer('laya-compile', TARGET, now=1)
        self.queue.offer('laya-sync', TARGET, now=2)
        with self.queue.connect() as db:
            db.execute("UPDATE wakes SET status='running'")
        calls = []
        with mock.patch.object(runtime_work, 'sample', side_effect=AssertionError('admission sampled')):
            result = runtime_work.drain(self.queue, execute=lambda row: calls.append(row['kind']) or True,
                                        clock=lambda: 0, wall=lambda: 5, sleep=lambda _s: None, max_runs=2)
        self.assertEqual(result, {'status': 'pending', 'runs': 2})
        self.assertEqual(calls, ['laya-compile', 'laya-sync'])
        self.assertEqual([row['kind'] for row in self.queue.pending()], ['learning'])


if __name__ == '__main__':
    unittest.main()
