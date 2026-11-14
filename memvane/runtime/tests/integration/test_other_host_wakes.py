"""Proof F5: a host below writerFloor or of another major leaves the wake row as it was."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.runtime import runtime_work
from memvane.runtime.types import DispatchFailure

TARGET = '/synthetic/project'


class OtherHostWakes(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvh-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.root.chmod(0o700)
        self.queue = runtime_work.WakeQueue(self.root)
        self.calls = []

    def row(self):
        rows = self.queue.pending()
        self.assertEqual(len(rows), 1)
        return {key: rows[0][key] for key in ('status', 'due', 'attempts', 'reason', 'revision')}

    def drain(self, execute, wall):
        return runtime_work.drain(self.queue, execute=execute, ready=lambda: True,
                                  clock=lambda: 0, wall=lambda: wall, sleep=lambda _s: None,
                                  max_runs=8)

    def refuse(self, code):
        def execute(row):
            self.calls.append(row['revision'])
            raise DispatchFailure(code)
        return execute

    def test_rows_stay_untouched_for_a_newer_host(self):
        for code in ('writer_floor', 'state_format_mismatch'):
            with self.subTest(code=code):
                self.calls.clear()
                self.queue.offer('laya-sync', TARGET, now=100)
                before = self.row()
                result = self.drain(self.refuse(code), wall=200)
                # One attempt, then the row is skipped for this host: no due push, no count.
                self.assertEqual((result['runs'], len(self.calls)), (1, 1))
                self.assertEqual(self.row(), before)
                for _ in range(3):
                    self.assertEqual(self.drain(self.refuse(code), wall=250)['runs'], 0)
                self.assertEqual(self.row(), before)
                # This host starts no worker for it; the lease-free timer only waits.
                with mock.patch.object(runtime_work.subprocess, 'Popen') as spawned, \
                     mock.patch.object(runtime_work, '_arm_timer') as armed, \
                     mock.patch('time.time', return_value=250.0):
                    self.assertEqual(runtime_work._wake(self.queue), 'idle')
                self.assertEqual((spawned.call_count, armed.call_count), (0, 1))
                woken, clock = [], [0.0]
                runtime_work.timer(self.queue, wake_once=woken.append, wall=lambda: 250,
                                   clock=lambda: clock[0],
                                   sleep=lambda s: clock.__setitem__(0, clock[0] + s), max_seconds=60)
                self.assertEqual(woken, [])
                # A newer host runs the same row at its recorded due and ACKs it.
                with mock.patch.object(runtime_work, '_plugin_version', return_value='99.0.0'):
                    self.assertEqual(self.drain(lambda row: True, wall=250)['runs'], 1)
                self.assertEqual(self.queue.pending(), [])

    def test_a_new_revision_is_tried_again(self):
        self.queue.offer('laya-sync', TARGET, now=100)
        self.drain(self.refuse('writer_floor'), wall=200)
        self.queue.offer('laya-sync', TARGET, now=210)
        before = self.row()
        self.assertEqual(self.drain(self.refuse('writer_floor'), wall=220)['runs'], 1)
        self.assertEqual(self.row(), before)
        self.assertEqual(len(self.calls), 2)
        # After the recheck time this host looks once more, still without touching the row.
        self.assertEqual(self.drain(self.refuse('writer_floor'), wall=220 + 301)['runs'], 1)
        self.assertEqual(self.row(), before)


if __name__ == '__main__':
    unittest.main()
