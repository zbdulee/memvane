"""Pin runtime_work wake semantics through the runtime_wake_policy seam.

Synthetic only: a disposable private root, no QMD, daemon, model, network or
real handler. drain() always receives injected execute/ready/clock/wall/sleep,
so sample() and dispatch() are never reached.
"""
import ast
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

import runtime_wake_policy as policy  # noqa: E402
import runtime_work as runtime  # noqa: E402
from context_learning.local_cycle import ResourceDeferred  # noqa: E402
from context_learning.resource_coordination import _lock  # noqa: E402

HEX = '0123456789abcdef' * 2


class PolicyPurity(unittest.TestCase):
    def test_policy_module_imports_no_io_or_process_machinery(self):
        tree = ast.parse((CORE / 'runtime_wake_policy.py').read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or '').split('.')[0])
        self.assertLessEqual(names, {'__future__', 'hashlib', 'pathlib'})

    def test_runtime_work_reexports_the_same_objects(self):
        self.assertIs(runtime.KINDS, policy.KINDS)
        self.assertIs(runtime.RETRY, policy.RETRY)
        self.assertEqual(runtime.MAX_WAKES, policy.MAX_WAKES)


class PolicyDecisions(unittest.TestCase):
    def test_fixed_kinds_and_priority_classes(self):
        self.assertEqual(policy.KINDS, {'index': 0, 'update': 1, 'cleanup': 2,
                                        'laya-sync': 2, 'laya-compile': 2,
                                        'learning': 3, 'auto': 3})

    def test_validate_accepts_only_fixed_shapes(self):
        for kind, target in (('index', ''), ('update', HEX), ('learning', '/synthetic/a'),
                             ('auto', '/synthetic/a'), ('cleanup', '/synthetic/a'),
                             ('laya-sync', '/synthetic/a'),
                             ('laya-compile', '/synthetic/a')):
            policy.validate_wake(kind, target)
        bad = (('unknown', ''), ('index', 'x'), ('index', None), ('update', HEX[:31]),
               ('update', HEX.upper()), ('learning', 'relative'), ('learning', ''),
               ('cleanup', '/a\x00b'), ('auto', '/' + 'a' * 4096))
        for kind, target in bad:
            with self.assertRaises(ValueError, msg=(kind, target)) as caught:
                policy.validate_wake(kind, target)
            self.assertEqual(str(caught.exception), 'invalid_runtime_wake')

    def test_key_coalesces_by_kind_and_target(self):
        self.assertEqual(policy.wake_key('learning', '/a'), policy.wake_key('learning', '/a'))
        self.assertNotEqual(policy.wake_key('learning', '/a'), policy.wake_key('auto', '/a'))
        self.assertEqual(len(policy.wake_key('index', '')), 64)

    def test_capacity_reserves_one_slot_for_index(self):
        self.assertEqual(policy.capacity_limit('index'), 256)
        for kind in ('update', 'cleanup', 'laya-sync', 'laya-compile', 'learning', 'auto'):
            self.assertEqual(policy.capacity_limit(kind), 255)

    def test_backoff_table_is_unchanged(self):
        self.assertEqual([policy.retry_delay(n) for n in range(9)], [5, 10, 20, 40, 80, 160, 300, 300, 300])

    def test_handler_status_ack_predicate(self):
        for status in ('busy', 'deferred_resource', 'deferred_quota',
                       'staged_evaluation_deferred', 'apply_deferred',
                       'setup_required', 'setup_in_progress'):
            self.assertFalse(policy.handler_done(status), status)
        for status in ('ok', 'trained', 'skipped', 'disabled'):
            self.assertTrue(policy.handler_done(status), status)

    def test_cleanup_ack_predicate(self):
        self.assertFalse(policy.cleanup_done({'reason': 'resource_deferred'}))
        self.assertTrue(policy.cleanup_done({'reason': 'cooldown'}))
        self.assertTrue(policy.cleanup_done({}))

    def test_next_row_orders_by_kind_class_then_due_then_id(self):
        rows = [{'kind': 'learning', 'due': 0, 'id': 'a'}, {'kind': 'laya-sync', 'due': 9, 'id': 'b'},
                {'kind': 'index', 'due': 9, 'id': 'z'}, {'kind': 'update', 'due': 0, 'id': 'c'}]
        self.assertEqual(policy.next_row(rows)['kind'], 'index')
        same = [{'kind': 'auto', 'due': 2, 'id': 'a'}, {'kind': 'learning', 'due': 1, 'id': 'b'},
                {'kind': 'learning', 'due': 1, 'id': 'a'}]
        self.assertEqual(policy.next_row(same), {'kind': 'learning', 'due': 1, 'id': 'a'})
        self.assertEqual(policy.due_rows(rows, 5), [rows[0], rows[3]])

    def test_idle_wait_never_passes_deadline(self):
        self.assertEqual([policy.idle_wait(r) for r in (60, 5, 2.5, 0, -1)], [5, 5, 2.5, 0, 0])

    def test_settle_ack_and_retry(self):
        self.assertEqual(policy.settle(3, done=True, now=10),
                         {'ack': True, 'due': 10, 'reason': None, 'count': 0})
        self.assertEqual(policy.settle(0, done=False, now=10),
                         {'ack': False, 'due': 15, 'reason': 'pending', 'count': 1})
        self.assertEqual(policy.settle(2, done=None, now=10),
                         {'ack': False, 'due': 30, 'reason': 'pending', 'count': 1})
        self.assertEqual(policy.settle(0, done=False, error=RuntimeError('x'), now=10),
                         {'ack': False, 'due': 15, 'reason': 'RuntimeError', 'count': 1})
        deferred = policy.DispatchFailure('target_deferred', 'a' * 40, 'c' * 64)
        self.assertEqual(policy.settle(0, done=False, error=deferred, now=10),
                         {'ack': False, 'due': 15, 'count': 1,
                          'reason': 'dispatch:target_deferred:' + 'a' * 40 + ':' + 'c' * 64})
        with self.assertRaises(ValueError):
            policy.DispatchFailure('private/path', 'a' * 40)


class DrainThroughSeam(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': ''})
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.queue = runtime.WakeQueue(self.root)

    def drain(self, execute, *, wall=100, ready=lambda: True, max_runs=1, **kw):
        return runtime.drain(self.queue, execute=execute, ready=ready, clock=lambda: 0,
                             wall=lambda: wall, sleep=lambda _: None, max_runs=max_runs, **kw)

    def rows(self):
        return self.queue.pending()

    def test_schema_is_unchanged(self):
        self.queue.put('index', '', now=0)
        with sqlite3.connect(self.queue.path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(wakes)')]
        self.assertEqual(columns, ['id', 'kind', 'target', 'revision', 'status', 'due', 'attempts', 'reason'])

    def test_new_laya_request_contends_for_old_uid_worker_without_new_queue(self):
        lease = runtime._lock(self.root / 'runtime-worker.lock')
        self.addCleanup(os.close, lease)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spawned = []
        with patch.object(runtime, 'default_root', return_value=self.root), \
             patch.object(runtime.subprocess, 'Popen', side_effect=lambda *args, **kw: spawned.append((args, kw))):
            result = runtime.request('laya-sync', '/synthetic/a')
        self.assertEqual(result, {'status': 'queued'})
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][0][0][3], 'handoff')
        self.assertEqual(len(self.rows()), 1)

    def test_handoff_waiter_takes_released_uid_lease(self):
        owner = runtime._lock(self.root / 'runtime-worker.lock')
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handoff = runtime._lock(self.root / 'runtime-worker-handoff.lock')
        released = threading.Timer(.05, lambda: os.close(owner))
        released.start()
        observed = []
        try:
            with patch.object(runtime, '_drain_and_rewake',
                              side_effect=lambda queue, fd: observed.append(queue.root)):
                runtime._handoff_worker(self.queue, handoff)
        finally:
            released.join()
        self.assertEqual(observed, [self.root])

    def test_old_self_rewake_and_new_waiter_in_separate_processes(self):
        lock = self.root / 'runtime-worker.lock'
        ready, taken = self.root / 'old-ready', self.root / 'new-took-lease'
        old_code = '''import fcntl,os,sys,time
fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
open(sys.argv[2],'w').close()
time.sleep(.4)
os.close(fd)
for _ in range(5):
    fd=os.open(sys.argv[1],os.O_RDWR)
    try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: pass
    else: time.sleep(.03)
    os.close(fd)
'''
        new_code = '''import sys
sys.path.insert(0,sys.argv[1])
import runtime_work as runtime
from pathlib import Path
root=Path(sys.argv[2]); queue=runtime.WakeQueue(root)
fd=runtime._lock(root/'runtime-worker-handoff.lock')
runtime._drain_and_rewake=lambda _queue,_fd: Path(sys.argv[3]).write_text('new')
runtime._handoff_worker(queue,fd)
'''
        old = subprocess.Popen([sys.executable, '-B', '-c', old_code,
                                str(lock), str(ready)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
        new = None
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            new = subprocess.Popen([sys.executable, '-B', '-c', new_code,
                                    str(CORE), str(self.root), str(taken)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, errors = new.communicate(timeout=5)
            self.assertEqual(new.returncode, 0, errors.decode())
            self.assertEqual(taken.read_text(), 'new')
            _, errors = old.communicate(timeout=5)
            self.assertEqual(old.returncode, 0, errors.decode())
        finally:
            for child in (new, old):
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=2)

    def test_failed_laya_dispatch_retains_revision_and_fixed_reason(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        original = self.rows()[0]['revision']
        def failed(_row):
            raise policy.DispatchFailure('target_child_failed', 'b' * 40, 'd' * 64)
        self.drain(failed)
        retained = self.rows()[0]
        self.assertEqual(retained['revision'], original)
        self.assertEqual(retained['attempts'], 1)
        self.assertEqual(retained['reason'], 'dispatch:target_child_failed:' + 'b' * 40 + ':' + 'd' * 64)

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
        self.assertIsNotNone(self.queue.put('index', '', now=0))
        self.assertIsNone(self.queue.put('update', HEX, now=0))
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
        self.assertEqual(result, {'status': 'pending', 'runs': 0})
        self.assertEqual(slept, [5, 2])

    def test_exception_records_type_name(self):
        self.queue.put('cleanup', '/synthetic/a', now=0)

        def boom(_):
            raise RuntimeError('handler')
        self.assertEqual(self.drain(boom), {'status': 'pending', 'runs': 1})
        row, = self.rows()
        self.assertEqual((row['reason'], row['attempts'], row['due']), ('RuntimeError', 1, 105))

    def test_ack_deletes_only_the_claimed_revision(self):
        self.queue.put('index', '', now=0)

        def rerequest(_):
            self.queue.put('index', '', now=0)
            return True
        self.drain(rerequest, wall=50)
        row, = self.rows()
        self.assertEqual((row['revision'], row['status'], row['due'], row['attempts']), (2, 'pending', 50, 0))
        self.assertEqual(self.drain(lambda _: True, wall=60), {'status': 'drained', 'runs': 1})

    def failing(self, code, *, detail=None):
        def execute(_row):
            raise policy.DispatchFailure(code, 'a' * 40, 'c' * 64, detail=detail)
        return execute

    def test_preparing_owner_wait_does_not_grow_the_backoff(self):
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        self.drain(self.failing('target_preparing'), wall=100)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['due']), (0, 100 + policy.RETRY_BASE_SECS))
        self.assertTrue(row['reason'].startswith('dispatch:target_preparing:'))
        for wall in (200, 300, 400):
            self.drain(self.failing('target_child_failed'), wall=wall)
        self.assertEqual(self.rows()[0]['attempts'], 3)
        # Prior real failures stay counted; the wait itself adds none and
        # returns after the owner's own spawn backoff.
        self.drain(self.failing('target_preparing'), wall=1000)
        row, = self.rows()
        self.assertEqual((row['attempts'], row['due']), (3, 1000 + policy.RETRY_BASE_SECS))
        self.drain(self.failing('target_deferred'), wall=2000)
        self.assertEqual(self.rows()[0]['attempts'], 4)

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
             patch.object(runtime, 'wake', return_value=True):
            self.assertEqual(runtime.request('laya-sync', '/synthetic/b', pull_due=True),
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
        self.assertEqual(row['reason'], 'dispatch:target_child_failed:' + 'a' * 40 + ':' + 'c' * 64)
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
        self.queue.put('index', '', now=0)
        with patch.object(policy, 'retry_delay', return_value=42):
            self.drain(lambda _: False, wall=100)
        self.assertEqual(self.rows()[0]['due'], 142)

    def test_busy_lease_returns_without_running(self):
        self.queue.put('index', '', now=0)
        fd = _lock(self.root / 'runtime-worker.lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.drain(lambda _: self.fail('busy')), {'status': 'busy', 'runs': 0})
        finally:
            os.close(fd)
        self.assertEqual(len(self.rows()), 1)

    def test_resource_deferred_admission_waits_then_runs(self):
        self.queue.put('index', '', now=0)
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

    def test_index_update_and_laya_sync_dispatch_without_learning_admission(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        self.queue.put('update', HEX, now=0)
        self.queue.put('index', '', now=0)
        self.queue.put('laya-sync', '/synthetic/a', now=0)
        calls=[]
        with patch.object(runtime, 'sample', side_effect=AssertionError('learning admission sampled')):
            result=runtime.drain(self.queue, execute=lambda row: calls.append(row['kind']) or True,
                                 clock=lambda: 0, wall=lambda: 1, sleep=lambda _: None,
                                 max_runs=3)
        self.assertEqual(result, {'status':'pending','runs':3})
        self.assertEqual(calls, ['index','update','laya-sync'])
        self.assertEqual([row['kind'] for row in self.rows()], ['learning'])

    def test_running_rows_are_recovered_and_index_runs_first(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        self.queue.put('index', '', now=1)
        with self.queue.connect() as db:
            db.execute("UPDATE wakes SET status='running'")
        calls = []
        self.drain(lambda row: calls.append(row['kind']) or True, max_runs=2)
        self.assertEqual(calls, ['index', 'learning'])
        self.assertEqual(self.rows(), [])

    def test_bounded_worker_rewakes_retained_rows_even_after_failure(self):
        self.queue.put('learning', '/synthetic/a', now=0)
        for outcome in ({'status': 'pending', 'runs': 8}, RuntimeError('worker failed')):
            with self.subTest(outcome=type(outcome).__name__):
                side_effect = outcome if isinstance(outcome, Exception) else None
                with patch.object(runtime, 'drain', side_effect=side_effect,
                                  return_value=outcome) as draining:
                    with patch.object(runtime, 'wake') as rewake:
                        if isinstance(outcome, Exception):
                            with self.assertRaises(RuntimeError):
                                runtime._drain_and_rewake(self.queue, 123)
                        else:
                            self.assertEqual(runtime._drain_and_rewake(self.queue, 123), outcome)
                        draining.assert_called_once_with(self.queue, lease_fd=123)
                        rewake.assert_called_once_with()
            self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main(verbosity=1)
