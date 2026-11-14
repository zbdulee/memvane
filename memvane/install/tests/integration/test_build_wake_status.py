"""Rereview C1 cleanup: 1.0 has no build wake cancel; the status read stays read-only."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from memvane.install import api as install
from memvane.runtime import api as runtime
from memvane.runtime import runtime_work

TARGET = '/synthetic/project'


class BuildWakeStatus(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvi-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.root.chmod(0o700)
        self.queue = runtime_work.WakeQueue(self.root)

    def tables(self):
        db = sqlite3.connect(self.queue.path.as_uri() + '?mode=ro', uri=True)
        try:
            return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()

    def test_status_reads_the_row_and_no_cancel_path_remains(self):
        self.queue.offer('laya-sync', TARGET, now=100)
        before = self.tables()
        for _ in range(2):
            status = install.wake_status(TARGET, queue=self.queue)
            self.assertEqual((status['wake']['kind'], status['wake']['target'], status['wake']['status']),
                             ('laya-sync', TARGET, 'pending'))
            self.assertEqual((status['overflow'], status['cancellations']), (False, []))
        self.assertEqual(len(self.queue.pending()), 1)
        self.assertEqual(self.tables(), before)
        self.assertNotIn('wake_cancellations', before)
        self.assertEqual([name for name in ('cancel_wake',) if hasattr(install, name)], [])
        self.assertEqual([name for name in ('cancel_pending_wake',) if hasattr(runtime, name)], [])


if __name__ == '__main__':
    unittest.main()
