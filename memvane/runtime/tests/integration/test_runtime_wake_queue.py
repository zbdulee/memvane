import os
import sqlite3
from tests.support.case import FixtureCase
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import runtime_work as runtime


class DrainThroughSeam(FixtureCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "state"
        self.root.mkdir(mode=0o700)
        self.queue = runtime.WakeQueue(self.root)

    def drain(self, execute, *, wall=100, ready=lambda: True, max_runs=1, **kw):
        return runtime.drain(self.queue, execute=execute, ready=ready, clock=lambda: 0,
                             wall=lambda: wall, sleep=lambda _: None, max_runs=max_runs, **kw)


    def rows(self):
        return self.queue.pending()


    def test_schema_is_unchanged(self):
        self.queue.put('laya-sync', '/synthetic/b', now=0)
        with sqlite3.connect(self.queue.path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(wakes)')]
        self.assertEqual(columns, ['id', 'kind', 'target', 'revision', 'status', 'due', 'attempts', 'reason'])


    def test_failed_laya_dispatch_retains_revision_and_fixed_reason(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        original = self.rows()[0]['revision']
        def failed(_row):
            raise policy.DispatchFailure('target_child_failed')
        self.drain(failed)
        retained = self.rows()[0]
        self.assertEqual(retained['revision'], original)
        self.assertEqual(retained['attempts'], 1)
        self.assertEqual(retained['reason'], 'dispatch:target_child_failed')


    def test_laya_handler_receives_existing_uid_lease_for_owned_child(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        observed = []
        def handler(row):
            info = os.fstat(row['leaseFd'])
            expected = (self.root / 'runtime-worker.lock').stat()
            observed.append((info.st_dev, info.st_ino))
            self.assertEqual(observed[-1], (expected.st_dev, expected.st_ino))
            return True
        self.assertEqual(self.drain(handler), {'status': 'drained', 'runs': 1})
        self.assertEqual(len(observed), 1)


    def test_put_rejects_through_policy_and_reports_capacity(self):
        with self.assertRaises(ValueError):
            self.queue.put('index', 'x', now=0)
        for n in range(255):
            self.assertIsNotNone(self.queue.put('learning', '/synthetic/%d' % n, now=0))
        self.assertIsNone(self.queue.put('auto', '/synthetic/extra', now=0))
        self.assertTrue((self.root / 'runtime-wake-overflow.json').exists())
        # An existing wake still coalesces at capacity.
        self.assertIsNotNone(self.queue.put('learning', '/synthetic/0', now=0))


    def test_falsy_result_backs_off_and_counts_attempts(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        self.drain(lambda _: False, wall=100)
        row, = self.rows()
        self.assertEqual((row['status'], row['due'], row['attempts'], row['reason']), ('pending', 105, 1, 'pending'))
        self.drain(lambda _: False, wall=105)
        row, = self.rows()
        self.assertEqual((row['due'], row['attempts'], row['revision']), (115, 2, 1))


    def test_quota_deferred_learning_wake_is_retained_for_retry(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        self.assertEqual(self.drain(lambda _: policy.handler_done('deferred_quota'), wall=100),
                         {'status': 'pending', 'runs': 1})
        row, = self.rows()
        self.assertEqual((row['kind'], row['status'], row['due'], row['attempts']),
                         ('learning', 'pending', 105, 1))


    def test_not_due_wake_is_not_run(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        self.drain(lambda _: False, wall=100)
        clock, slept = [0.], []

        def sleep(dt):
            slept.append(dt)
            clock[0] += dt
        result = runtime.drain(self.queue, execute=lambda _: self.fail('not due'), ready=lambda: True,
                               clock=lambda: clock[0], wall=lambda: 104, sleep=sleep, max_seconds=7)
        # Only a backed-off row: return at once and free the lease, no idle wait.
        self.assertEqual(result, {'status': 'pending', 'runs': 0})
        self.assertEqual(slept, [])


    def test_exception_records_type_name(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)

        def boom(_):
            raise RuntimeError('handler')
        self.assertEqual(self.drain(boom), {'status': 'pending', 'runs': 1})
        row, = self.rows()
        self.assertEqual((row['reason'], row['attempts'], row['due']), ('RuntimeError', 1, 105))


    def test_ack_deletes_only_the_claimed_revision(self):
        self.queue.put('laya-sync', '/synthetic/b', now=0)

        def rerequest(_):
            self.queue.put('laya-sync', '/synthetic/b', now=0)
            return True
        self.drain(rerequest, wall=50)
        row, = self.rows()
        self.assertEqual((row['revision'], row['status'], row['due'], row['attempts']), (2, 'pending', 50, 0))
        self.assertEqual(self.drain(lambda _: True, wall=60), {'status': 'drained', 'runs': 1})


    def failing(self, code, *, detail=None):
        def execute(_row):
            raise policy.DispatchFailure(code, detail=detail)
        return execute


    def test_preparing_owner_wait_does_not_grow_the_backoff(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        self.drain(self.failing('target_preparing'), wall=100)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['due']), (0, 100 + policy.PREPARING_RETRY_SECS))
        self.assertTrue(row['reason'].startswith('dispatch:target_preparing'))
        for wall in (200, 300, 400):
            self.drain(self.failing('target_child_failed'), wall=wall)
        self.assertEqual(self.rows()[0]['attempts'], 3)
        # Prior real failures stay counted; the wait itself adds none and
        # returns after the fixed preparing delay.
        self.drain(self.failing('target_preparing'), wall=1000)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['due']), (3, 1000 + policy.PREPARING_RETRY_SECS))
        self.drain(self.failing('target_deferred'), wall=2000)
        self.assertEqual(self.rows()[0]['attempts'], 4)
