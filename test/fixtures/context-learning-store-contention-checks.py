"""Short write transactions from another process must not fail CLI work or
abort a paid teacher call; the blocking prompt hook must still not wait.

A competing writer is simulated with a second connection holding
BEGIN IMMEDIATE. Lock windows are driven by events, not wall-clock sleeps,
so outcomes do not depend on machine load.
"""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from context_learning.contracts import digest, request
import context_learning.jobs as jobs_module
import context_learning.store as store_module
from context_learning.jobs import JobLease, cancel_job, job_status
from context_learning.store import capture, database


class LockHolder:
    """Hold the DB write lock from another connection until released."""
    def __init__(self, path):
        self.path = path
        self.held = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        db = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.held.set()
            self.release.wait(30)
            db.execute('COMMIT')
        finally:
            db.close()

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(10)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(30)


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qmd-store-contention-')
        self.state = Path(self.tmp.name) / 'private'
        self.state.mkdir(mode=0o700)
        self.db_path = self.state / 'learning.sqlite3'
        self.sample = request({'prompt': 'Does approval precede access?'},
            [{'candidate_id': 'card', 'revision_sha256': digest('body'),
              'excerpt': 'body', 'eligible': True}], host='codex', request_id='req')
        self.assertEqual(capture(self.sample, enabled=True, state_dir=self.state), 'stored')

    def tearDown(self):
        self.tmp.cleanup()

    def connect_timeouts(self, action):
        seen = []
        real = sqlite3.connect

        def spy(*args, **kwargs):
            seen.append(kwargs.get('timeout'))
            return real(*args, **kwargs)
        with patch.object(store_module.sqlite3, 'connect', side_effect=spy):
            action()
        return seen

    def test_cli_waits_for_a_short_competing_transaction(self):
        # Released from a timer; the caller must wait instead of failing with
        # "database is locked" (the old 50 ms busy timeout).
        with LockHolder(self.db_path) as holder:
            threading.Timer(0.3, holder.release.set).start()
            self.assertEqual(job_status(self.state, 'req'), {'status': 'not_found'})
        self.assertEqual(self.connect_timeouts(lambda: job_status(self.state, 'req')),
                         [store_module.BUSY_TIMEOUT])

    def test_prompt_hook_capture_keeps_the_short_timeout_and_fails_open(self):
        self.assertEqual(self.connect_timeouts(
            lambda: capture(self.sample, enabled=True, state_dir=self.state)),
            [store_module.HOOK_BUSY_TIMEOUT])
        self.assertLess(store_module.HOOK_BUSY_TIMEOUT, store_module.BUSY_TIMEOUT)
        with LockHolder(self.db_path):
            self.assertEqual(capture(self.sample, enabled=True, state_dir=self.state),
                             'storage_unavailable')

    def test_cancel_probe_survives_a_transient_lock(self):
        with JobLease(self.state, 'req') as lease:
            self.assertEqual(self.connect_timeouts(lease.cancelled),
                             [jobs_module.CANCEL_PROBE_TIMEOUT])
            lease.next_check = 0
            with LockHolder(self.db_path):
                lease.next_check = 0
                self.assertIs(lease.cancelled(), False)  # not raised
                self.assertIsNotNone(lease.busy_since)
            lease.next_check = 0
            self.assertIs(lease.cancelled(), False)
            self.assertIsNone(lease.busy_since)
            # A real cancellation is still delivered after the contention.
            self.assertEqual(cancel_job(self.state, 'req'), {'status': 'cancel_requested'})
            lease.next_check = 0
            self.assertIs(lease.cancelled(), True)
            lease.result_status = 'cancelled'

    def test_cancel_probe_gives_up_on_a_persistent_lock(self):
        with JobLease(self.state, 'req') as lease:
            with patch.object(jobs_module, 'CANCEL_PROBE_GRACE', 0), \
                    LockHolder(self.db_path):
                lease.next_check = 0
                with self.assertRaises(sqlite3.OperationalError):
                    lease.cancelled()

    def test_cancel_probe_does_not_swallow_other_database_errors(self):
        with JobLease(self.state, 'req') as lease:
            lease.next_check = 0
            with patch.object(jobs_module, 'database',
                              side_effect=sqlite3.OperationalError('disk I/O error')):
                with self.assertRaises(sqlite3.OperationalError):
                    lease.cancelled()
            self.assertIsNone(lease.busy_since)


if __name__ == '__main__':
    unittest.main()
