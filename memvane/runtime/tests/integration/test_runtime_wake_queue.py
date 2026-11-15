import fcntl
import os
from contextlib import closing
from unittest.mock import patch
import sqlite3
import select
import subprocess
import sys
import threading
from tests.support.case import FixtureCase
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import api
from memvane.runtime import runtime_work as runtime
from memvane.runtime.resource_coordination import _lock
from memvane.runtime.types import ResourceDeferred


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
        with closing(sqlite3.connect(self.queue.path)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(wakes)')]
        self.assertEqual(columns, ['id', 'kind', 'target', 'revision', 'status', 'due', 'attempts', 'reason'])

    def test_wake_status_reads_only_the_requested_row(self):
        self.assertEqual(api.wake_cancel_status('laya-sync', '/synthetic/a', queue=self.queue),
                         {'wake': None, 'cancellations': []})
        self.queue.put('laya-sync', '/synthetic/a', now=10)
        self.queue.put('laya-compile', '/synthetic/b', now=10)
        status = api.wake_cancel_status('laya-sync', '/synthetic/a', queue=self.queue)
        self.assertEqual(status['wake']['target'], '/synthetic/a')
        self.assertFalse(status['overflow'])
        self.assertEqual(status['cancellations'], [])
        self.assertEqual(len(self.rows()), 2)

    def test_revisions_stay_unique_after_ack_and_recreate(self):
        self.queue.put('learning', '/synthetic/p', now=0)
        first = self.rows()[0]['revision']
        with self.queue.connect() as db:
            db.execute('DELETE FROM wakes')
        self.queue.put('learning', '/synthetic/p', now=0)
        self.assertGreater(self.rows()[0]['revision'], first)

    def test_capacity_deferred_wake_is_retained_then_promoted(self):
        with patch.object(runtime, 'MAX_WAKES', 3), patch.object(policy, 'MAX_OVERFLOW', 1):
            self.assertEqual(self.queue.offer('learning', '/a', now=0), 'queued')
            self.assertEqual(self.queue.offer('learning', '/b', now=0), 'queued')
            self.assertEqual(self.queue.offer('learning', '/c', now=0), 'retained')
            self.assertEqual(self.queue.offer('learning', '/c', now=1), 'retained')
            self.assertEqual(self.queue.offer('learning', '/d', now=0), 'dropped')
            self.assertEqual(sorted(row['target'] for row in self.rows()), ['/a', '/b'])
            with self.queue.connect() as db:
                db.execute("DELETE FROM wakes WHERE target='/a'")
            self.assertEqual(sorted(row['target'] for row in self.rows()), ['/b', '/c'])
            with self.queue.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM wake_overflow').fetchone()[0], 0)


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


    def lease_free(self, name='runtime-worker.lock'):
        fd = os.open(self.root / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def test_handoff_contender_releases_handoff_lock_once_it_holds_the_lease(self):
        handoff = runtime._lock(self.root / 'runtime-worker-handoff.lock')
        fcntl.flock(handoff, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed = []

        def draining(queue, fd, **kwargs):
            # Another open file description: flock conflicts even in-process.
            observed.append((self.lease_free('runtime-worker-handoff.lock'),
                             self.lease_free('runtime-worker.lock')))
        with patch.object(runtime, '_drain_and_rewake', side_effect=draining):
            runtime._handoff_worker(self.queue, handoff, execute=lambda _row: True)
        self.assertEqual(observed, [(True, False)])
        with self.assertRaises(OSError):
            os.fstat(handoff)

    def test_timer_ends_when_its_queue_is_gone_or_its_lifetime_is_spent(self):
        self.queue.put('learning', '/synthetic/a', now=10 ** 10)
        clock = [0.]
        outcome = runtime.timer(self.queue, wake_once=lambda _q: self.fail('not due'),
                                wall=lambda: 0, clock=lambda: clock[0],
                                sleep=lambda dt: clock.__setitem__(0, clock[0] + dt), max_seconds=40)
        self.assertEqual(outcome, 'expired')

        class Gone:
            def pending(self):
                raise OSError('moved')
        self.assertEqual(runtime.timer(Gone(), wake_once=lambda _q: None, sleep=lambda _: None), 'gone')

    def drops(self):
        import json
        return json.loads((self.root / runtime.DROP_DETAIL_FILE).read_text())

    def test_a_laya_wake_failing_up_to_the_cap_is_dropped_and_recorded(self):
        self.queue.put('laya-compile', '/synthetic/a', now=0)
        with self.queue.connect() as db:
            db.execute('UPDATE wakes SET attempts=?', (policy.RETRY_CAP_ATTEMPTS - 1,))
        self.drain(self.failing('target_deferred', detail='deferred:pending:unlabeled'), wall=100)
        self.assertEqual(self.rows(), [])
        entry, = self.drops().values()
        self.assertEqual((entry['reason'], entry['attempts'], entry['kind']),
                         ('retry_cap', policy.RETRY_CAP_ATTEMPTS, 'laya-compile'))
        self.assertTrue(entry['lastFailure'].startswith('dispatch:target_deferred'))
        # A later request starts a fresh history.
        self.queue.put('laya-compile', '/synthetic/a', now=200)
        self.assertEqual(self.rows()[0]['attempts'], 0)

    def test_preparing_that_never_ends_backs_off_and_is_reported_once(self):
        # A service that never adopts the install is reported after a bounded wait.
        stall = policy.PREPARING_STALL_SECS
        preparing = policy.DispatchFailure('target_preparing')
        early = policy.settle(0, done=False, error=preparing, now=100, kind='laya-sync',
                              preparing=(100 - stall + 1, 20))
        self.assertEqual((early['ack'], early['count'], early['due'], early.get('stalled')),
                         (False, 0, 100 + policy.PREPARING_RETRY_SECS, None))
        first = policy.settle(0, done=False, error=preparing, now=100, kind='laya-sync',
                              preparing=(100 - stall, 21))
        self.assertEqual((first['ack'], first['count'], first['due'], first['stalled']),
                         (False, 0, 100 + policy.RETRY_MAX_SECS, True))
        # Already reported for this streak: still backed off, not reported
        # again, and never dropped however long it lasts.
        later = policy.settle(policy.RETRY_CAP_ATTEMPTS * 3, done=False, error=preparing,
                              now=100 + 86400 * 3, kind='laya-sync',
                              preparing=(100 - stall, 500), reported=True)
        self.assertEqual((later['ack'], later['count'], later['due'], later.get('stalled')),
                         (False, 0, 100 + 86400 * 3 + policy.RETRY_MAX_SECS, None))

    def test_query_wake_pulls_a_backed_off_due_forward_without_resetting(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        for wall in range(1000, 8000, 1000):
            self.drain(self.failing('target_child_failed'), wall=wall)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['due']), (7, 7000 + policy.RETRY_MAX_SECS))
        # A plain hook wake only bumps the revision.
        self.assertEqual(self.queue.offer('laya-sync', '/synthetic/a', now=7050), 'queued')
        self.assertEqual(self.rows()[0]['due'], 7000 + policy.RETRY_MAX_SECS)
        self.assertEqual(self.queue.offer('laya-sync', '/synthetic/a', now=7050, pull_due=True),
                         'queued')
        pulled, = self.rows()
        self.assertEqual((pulled['due'], pulled['attempts'], pulled['reason']),
                         (7050, 7, row['reason']))
        # Never pushes an earlier due later.
        self.queue.offer('laya-sync', '/synthetic/a', now=7200, pull_due=True)
        self.assertEqual(self.rows()[0]['due'], 7050)
        with patch.object(runtime, 'WakeQueue', return_value=self.queue), \
             patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': ''}):
            self.assertEqual(runtime.request('laya-sync', '/synthetic/b', pull_due=True, start=False),
                             {'status': 'queued'})

    def test_ack_of_a_rerequested_wake_resets_its_retry_history(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        for wall in (100, 200):
            self.drain(self.failing('target_child_failed'), wall=wall)

        def rerequest(_):
            self.queue.put('laya-sync', '/synthetic/a', now=0)
            return True
        self.drain(rerequest, wall=1000)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['reason'], row['due']), (0, None, 1000))

    def test_failed_child_detail_is_kept_privately_and_bounded(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        detail = 'exit 1\n' + 'x' * 9000 + 'IndexContractError: provider_unavailable'
        self.drain(self.failing('target_child_failed', detail=detail), wall=100)
        row, = self.rows()
        self.assertEqual(row['reason'], 'dispatch:target_child_failed')
        path = self.root / runtime.FAILURE_DETAIL_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        import json
        saved = json.loads(path.read_text())[row['id']]
        self.assertEqual((saved['kind'], saved['target'], saved['reason']),
                         ('laya-sync', '/synthetic/a', row['reason']))
        self.assertEqual(len(saved['detail'].encode()), runtime.FAILURE_DETAIL_MAX_BYTES)
        self.assertTrue(saved['detail'].endswith('provider_unavailable'))
        # Bounded row count, newest kept.
        for n in range(runtime.FAILURE_DETAIL_MAX_ROWS + 3):
            self.queue.put('laya-sync', '/synthetic/n%d' % n, now=0)
            self.drain(self.failing('target_child_failed', detail='d%d' % n), wall=200 + n)
        kept = json.loads(path.read_text())
        self.assertEqual(len(kept), runtime.FAILURE_DETAIL_MAX_ROWS)
        self.assertIn('d%d' % (runtime.FAILURE_DETAIL_MAX_ROWS + 2),
                      [item['detail'] for item in kept.values()])

    def test_drain_goes_through_policy_settle(self):
        self.queue.put('laya-sync', '/synthetic/b', now=0)
        with patch.object(policy, 'retry_delay', return_value=42):
            self.drain(lambda _: False, wall=100)
        self.assertEqual(self.rows()[0]['due'], 142)

    def test_busy_lease_returns_without_running(self):
        self.queue.put('laya-sync', '/synthetic/b', now=0)
        fd = _lock(self.root / 'runtime-worker.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.drain(lambda _: self.fail('busy')), {'status': 'busy', 'runs': 0})
        finally:
            os.close(fd)
        self.assertEqual(len(self.rows()), 1)

    def test_resource_deferred_admission_waits_then_runs(self):
        self.queue.put('laya-sync', '/synthetic/b', now=0)
        signals = iter([ResourceDeferred('synthetic'), True])
        slept = []

        def ready():
            value = next(signals)
            if isinstance(value, Exception):
                raise value
            return value
        result = runtime.drain(self.queue, execute=lambda _: True, ready=ready, clock=lambda: 0,
                               wall=lambda: 1, sleep=slept.append, max_runs=1, max_seconds=60)
        self.assertEqual(result, {'status': 'drained', 'runs': 1})
        self.assertEqual(slept, [5])

    def test_bounded_worker_rewakes_retained_rows_even_after_failure(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        execute = lambda _row: True
        for outcome in ({'status': 'pending', 'runs': 8}, RuntimeError('worker failed')):
            with self.subTest(outcome=type(outcome).__name__):
                side_effect = outcome if isinstance(outcome, Exception) else None
                with patch.object(runtime, 'drain', side_effect=side_effect,
                                  return_value=outcome) as draining:
                    with patch.object(runtime, 'wake') as rewake:
                        if isinstance(outcome, Exception):
                            with self.assertRaises(RuntimeError):
                                runtime._drain_and_rewake(self.queue, 123, execute=execute)
                        else:
                            self.assertEqual(runtime._drain_and_rewake(self.queue, 123, execute=execute), outcome)
                        draining.assert_called_once_with(self.queue, lease_fd=123, execute=execute,
                                                         on_drop=None, on_stall=None)
                        rewake.assert_called_once_with()
            self.assertEqual(len(self.rows()), 1)

    def test_backed_off_rows_arm_only_a_timer_and_due_rows_start_worker(self):
        self.queue.put('laya-sync', '/synthetic/a', now=200)
        with patch.object(runtime.time, 'time', return_value=100), \
                patch.object(runtime.subprocess, 'Popen') as spawn:
            self.assertEqual(runtime._wake(self.queue), 'idle')
            self.assertIn('timer', spawn.call_args.args[0])
            spawn.reset_mock()
            self.queue.put('laya-sync', '/synthetic/b', now=90)
            self.assertEqual(runtime._wake(self.queue), 'spawned')
            self.assertIn('worker', spawn.call_args.args[0])

    def test_timer_lock_prevents_duplicate_timer(self):
        self.queue.put('laya-sync', '/synthetic/a', now=200)
        held = runtime._lock(self.root / runtime.TIMER_LOCK)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(runtime.time, 'time', return_value=100), \
                    patch.object(runtime.subprocess, 'Popen') as spawn:
                self.assertEqual(runtime._wake(self.queue), 'idle')
                spawn.assert_not_called()
        finally:
            os.close(held)

    def test_delayed_reoffer_survives_ack_and_plain_offer_pulls_due(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        def rewake(_row):
            self.queue.offer('laya-sync', '/synthetic/a', now=100, delay=30)
            return True
        self.assertEqual(self.drain(rewake, wall=100), {'status': 'pending', 'runs': 1})
        row, = self.rows()
        self.assertEqual((row['due'], row['attempts'], row['reason']),
                         (130, 0, runtime.DELAYED_REASON))
        self.queue.offer('laya-sync', '/synthetic/a', now=110)
        self.assertEqual((self.rows()[0]['due'], self.rows()[0]['reason']), (110, None))
        self.assertEqual(self.drain(lambda _row: True, wall=110), {'status': 'drained', 'runs': 1})

    def test_preparing_streak_reports_once_then_clears(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        reports = []
        wall = 1000
        while wall < 1000 + policy.PREPARING_STALL_SECS + 3 * policy.RETRY_MAX_SECS:
            self.drain(self.failing('target_preparing'), wall=wall,
                       on_stall=lambda kind, target, **kw: reports.append((kind, target, kw)))
            row, = self.rows()
            self.assertEqual(row['attempts'], 0)
            wall = row['due']
        self.assertEqual(len(reports), 1)
        self.drain(self.failing('target_child_failed'), wall=wall)
        self.assertEqual(runtime._read_map(self.root / runtime.PREPARING_FILE), {})

    def test_vanished_temporary_target_is_dropped_with_a_record(self):
        self.queue.put('laya-sync', '/synthetic/gone', now=0)
        reports = []
        result = self.drain(lambda _row: policy.Dropped('target_missing'), wall=100,
                            on_drop=lambda kind, target, **kw: reports.append((kind, target, kw)))
        self.assertEqual(result, {'status': 'drained', 'runs': 1})
        self.assertEqual(self.rows(), [])
        entry, = self.drops().values()
        self.assertEqual((entry['kind'], entry['reason']), ('laya-sync', 'target_missing'))
        self.assertEqual(len(reports), 1)

    def test_missing_target_backs_off_then_is_dropped_after_a_day(self):
        self.queue.put('laya-sync', '/synthetic/gone', now=0)
        def missing(_row):
            raise policy.TargetMissing()
        self.drain(missing, wall=100)
        self.assertEqual(self.rows()[0]['attempts'], 0)
        for wall in (200, 300, 100 + policy.MISSING_DROP_SECS):
            self.drain(missing, wall=wall)
        self.assertEqual(self.rows(), [])
        entry, = self.drops().values()
        self.assertEqual(entry['reason'], 'target_missing')

    def test_new_laya_request_contends_for_old_uid_worker_without_new_queue(self):
        lease = runtime._lock(self.root / 'runtime-worker.lock')
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(runtime, 'WakeQueue', return_value=self.queue), \
                    patch.object(runtime, '_handoff', return_value=True) as handoff, \
                    patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': ''}):
                self.assertEqual(runtime.request('laya-sync', '/synthetic/a'), {'status': 'queued'})
                handoff.assert_called_once_with()
            self.assertEqual(len(self.rows()), 1)
        finally:
            os.close(lease)

    def test_handoff_waiter_takes_released_uid_lease(self):
        owner = runtime._lock(self.root / 'runtime-worker.lock')
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handoff = runtime._lock(self.root / 'runtime-worker-handoff.lock')
        fcntl.flock(handoff, fcntl.LOCK_EX | fcntl.LOCK_NB)
        waiting = threading.Event()
        real_flock = fcntl.flock
        def flock(fd, operation):
            if operation == fcntl.LOCK_EX:
                waiting.set()
            return real_flock(fd, operation)
        def release():
            waiting.wait()
            os.close(owner)
        thread = threading.Thread(target=release)
        observed = []
        thread.start()
        try:
            with patch.object(runtime.fcntl, 'flock', side_effect=flock), \
                    patch.object(runtime, '_drain_and_rewake',
                                 side_effect=lambda queue, fd, **kw: observed.append(queue.root)):
                runtime._handoff_worker(self.queue, handoff, execute=lambda _row: True)
        finally:
            thread.join()
        self.assertEqual(observed, [self.root])

    def test_old_owner_and_new_waiter_exclude_each_other_across_processes(self):
        path = self.root / 'runtime-worker.lock'
        owner = runtime._lock(path)
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        script = ("import sys\n"
                  "from pathlib import Path\n"
                  "from memvane.runtime import runtime_work as r\n"
                  "root=Path(sys.argv[1]);q=r.WakeQueue(root)\n"
                  "fd=r._lock(root/'runtime-worker-handoff.lock')\n"
                  "r._drain_and_rewake=lambda q,fd,**kw: print('acquired',flush=True)\n"
                  "print('waiting',flush=True)\n"
                  "r._handoff_worker(q,fd,execute=lambda row: True)\n")
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def line():
            if not select.select([child.stdout], [], [], 5)[0]:
                self.fail('child did not reach lock boundary')
            return child.stdout.readline().strip()
        try:
            self.assertEqual(line(), 'waiting')
            os.close(owner)
            owner = None
            for _ in range(5):
                contender = runtime._lock(path)
                try:
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                finally:
                    os.close(contender)
            self.assertEqual(line(), 'acquired')
        finally:
            if owner is not None:
                os.close(owner)
            child.communicate('done', timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_yield_request_stops_drain_until_it_expires(self):
        from memvane.runtime.runtime_lease import atomic_json
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        self.queue.put('laya-sync', '/synthetic/b', now=0)
        calls = []
        def run(row):
            calls.append(row['target'])
            atomic_json(self.root / runtime.YIELD_FILE, {'until': 130})
            return True
        with patch.object(runtime.time, 'time', return_value=100), \
                patch.object(runtime.subprocess, 'Popen') as spawned:
            self.assertEqual(self.drain(run, wall=100, max_runs=2), {'status': 'yielded', 'runs': 1})
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(self.rows()), 1)
            self.assertEqual(runtime._wake(self.queue), 'yield')
            self.assertIn('timer', spawned.call_args.args[0])
            for until in (99, 100 + 10 ** 6):
                atomic_json(self.root / runtime.YIELD_FILE, {'until': until})
                self.assertFalse(runtime.yield_requested(self.root))

    def test_exiting_timer_rechecks_queue_after_releasing_lock(self):
        fd = runtime._lock(self.root / runtime.TIMER_LOCK)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def raced(queue, **_kw):
            queue.put('laya-sync', '/synthetic/a', now=200)
            return 'empty'
        with patch.object(runtime, 'timer', side_effect=raced), \
                patch.object(runtime, 'WakeQueue', return_value=self.queue), \
                patch.object(runtime, '_wake') as wake:
            self.assertEqual(runtime.main(['timer', str(fd)], execute=lambda _row: True), 0)
        with self.assertRaises(OSError):
            os.fstat(fd)
        wake.assert_called_once_with(self.queue)

    def test_transient_failure_retries_without_another_hook(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        self.drain(self.failing('target_child_failed'), wall=100)
        due = self.rows()[0]['due']
        wall = [100]
        runs = []
        def wake_once(queue):
            runtime.drain(queue, execute=lambda row: runs.append((row['target'], wall[0])) or True,
                          ready=lambda: True, clock=lambda: 0, wall=lambda: wall[0],
                          sleep=lambda _seconds: None, max_runs=1)
        self.assertEqual(runtime.timer(self.queue, wake_once=wake_once, wall=lambda: wall[0],
                                      clock=lambda: wall[0],
                                      sleep=lambda dt: wall.__setitem__(0, wall[0] + dt),
                                      max_seconds=60), 'empty')
        self.assertEqual(runs, [('/synthetic/a', due)])
        self.assertEqual(self.rows(), [])
