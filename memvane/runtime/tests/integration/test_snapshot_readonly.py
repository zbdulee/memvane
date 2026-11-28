"""``runtime_main(['snapshot-readonly'])``: the wake queue as the real writers left it, read without a write."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from tests.support.case import FixtureCase, account_home
from memvane.runtime import api
from memvane.runtime import runtime_work as runtime
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime.service_state import service_root
from memvane.runtime.types import DispatchFailure


def tree(root):
    """Every path under ``root`` with its bytes (files) and mtime; directories included."""
    root = Path(root)
    if not root.exists():
        return None
    found = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        found[str(path.relative_to(root))] = (path.read_bytes() if path.is_file() else None, info.st_mtime_ns)
    return found


class SnapshotReadonly(FixtureCase):
    def snapshot(self, *argv):
        seen = []
        code = api.runtime_main(['snapshot-readonly', *argv], execute=seen.append)
        self.assertEqual((code, len(seen)), (0, 1))
        return seen[0]

    def written_queue(self, root):
        """Rows, a capacity-retained offer and the diagnostics files the real writers write."""
        queue = runtime.WakeQueue(root)
        self.assertEqual(queue.offer('laya-sync', '/synthetic/a', now=10), 'queued')
        self.assertEqual(queue.offer('learning', '/synthetic/b', now=20, delay=30), 'queued')
        row = queue.pending()[0]
        runtime._record_failure_detail(queue.root, row, DispatchFailure('target_child_failed', detail='synthetic tail'), 40)
        self.assertTrue(runtime._record_tail(queue.root / runtime.PREPARING_FILE, row['id'],
                                             {'at': 41, 'seen': 1, 'target': row['target']}))
        with mock.patch.object(policy, 'capacity_limit', return_value=2):
            self.assertEqual(queue.offer('laya-sync', '/synthetic/c', now=50), 'retained')
        return queue

    def test_the_snapshot_carries_the_queue_and_files_and_changes_nothing(self):
        root = self.tmp / 'state'
        root.mkdir(mode=0o700)
        queue = self.written_queue(root)
        with queue.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM wakes ORDER BY due,id')]
            overflow = [dict(row) for row in db.execute('SELECT * FROM wake_overflow ORDER BY at,id')]
            meta = {row[0]: row[1] for row in db.execute('SELECT key,value FROM wake_meta')}
        before = tree(root)
        snap = self.snapshot(str(root))
        self.assertEqual(tree(root), before)
        self.assertEqual((snap['status'], snap['root']), ('read', str(root.resolve())))
        self.assertEqual((snap['wakes'], snap['overflowRows'], snap['meta']), (rows, overflow, meta))
        self.assertEqual((len(rows), len(overflow)), (2, 1))
        self.assertEqual(snap['overflow']['status'], 'deferred_queue_capacity')
        self.assertEqual(list(snap['preparing']), [rows[0]['id']])
        self.assertEqual(snap['failures'], runtime._read_map(root / runtime.FAILURE_DETAIL_FILE))
        self.assertEqual((snap['drops'], snap['deferred']), ({}, {}))

    def test_the_overflow_row_is_not_promoted_as_pending_would(self):
        root = self.tmp / 'state'
        root.mkdir(mode=0o700)
        queue = self.written_queue(root)
        snap = self.snapshot(str(root))
        self.assertEqual(len(snap['overflowRows']), 1)
        # The writer path moves it into the queue once there is room; the snapshot never does.
        self.assertEqual(len(self.snapshot(str(root))['overflowRows']), 1)
        queue.pending()
        self.assertEqual(self.snapshot(str(root))['overflowRows'], [])

    def test_a_missing_root_is_absent_and_never_created(self):
        missing = self.tmp / 'missing'
        snap = self.snapshot(str(missing))
        self.assertEqual((snap['status'], snap['wakes']), ('absent', []))
        self.assertFalse(missing.exists())
        # The default root resolves to the test runtime root's ``laya-inference``, not created either.
        before = tree(self.runtime)
        self.assertEqual(self.snapshot()['status'], 'absent')
        self.assertEqual(tree(self.runtime), before)
        self.assertFalse((self.runtime / 'laya-inference').exists())

    def test_the_default_root_reads_the_queue_its_writer_made(self):
        root = service_root(create=True)
        runtime.WakeQueue().offer('laya-sync', '/synthetic/d', now=5)
        before = tree(root)
        snap = self.snapshot()
        self.assertEqual((snap['status'], snap['root'], [row['target'] for row in snap['wakes']]),
                         ('read', str(root), ['/synthetic/d']))
        self.assertEqual(tree(root), before)

    def test_a_root_without_a_queue_reads_its_files_only(self):
        root = self.tmp / 'state'
        root.mkdir(mode=0o700)
        self.assertTrue(runtime._record_tail(root / runtime.DROP_DETAIL_FILE, 'k', {'at': 1, 'event': 'wake.drop'}))
        snap = self.snapshot(str(root))
        self.assertEqual((snap['status'], list(snap['drops'])), ('absent', ['k']))
        self.assertFalse((root / 'runtime-wakes.sqlite3').exists())

    def test_a_fixture_run_never_reads_the_account_queue(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('MEMVANE_TEST_RUNTIME_ROOT')
            self.assertEqual(self.snapshot()['status'], 'fixture_blocked')
        self.assertEqual(self.snapshot(str(account_home() / 'state'))['status'], 'fixture_blocked')
        self.assertEqual(self.snapshot('relative/root')['status'], 'fixture_blocked')

    def test_an_unsafe_root_or_queue_file_is_named_not_read(self):
        root = self.tmp / 'state'
        root.mkdir(mode=0o755)
        self.assertEqual(self.snapshot(str(root))['status'], 'unsafe_root')
        root.chmod(0o700)
        (root / 'runtime-wakes.sqlite3').write_bytes(b'')
        (root / 'runtime-wakes.sqlite3').chmod(0o644)
        snap = self.snapshot(str(root))
        self.assertEqual((snap['status'], snap['wakes']), ('unsafe_root', []))

    def test_other_argument_shapes_stay_unknown_commands(self):
        seen = []
        self.assertEqual(api.runtime_main(['snapshot-readonly', 'a', 'b'], execute=seen.append), 2)
        self.assertEqual(seen, [])
