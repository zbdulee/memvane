import ast
from pathlib import Path
import unittest
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import runtime_work as runtime

CORE = Path(__file__).resolve().parents[4]


class PolicyPurity(unittest.TestCase):
    def test_policy_module_imports_no_io_or_process_machinery(self):
        tree = ast.parse((CORE / 'memvane/runtime/runtime_wake_policy.py').read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or '')
        self.assertLessEqual(names, {'__future__', 'hashlib', 'pathlib', 'memvane.runtime.types'})

    def test_runtime_work_reexports_the_same_objects(self):
        self.assertIs(runtime.KINDS, policy.KINDS)
        self.assertIs(runtime.RETRY, policy.RETRY)
        self.assertEqual(runtime.MAX_WAKES, policy.MAX_WAKES)


class PolicyDecisions(unittest.TestCase):
    def test_sync_durable_progress_retries_without_counting_only_new_details(self):
        for reason in ('schema3_progress_ingest', 'schema3_progress_vector'):
            failure = policy.DispatchFailure('target_deferred',
                                             detail='deferred:pending:' + reason)
            attempts = 7
            for _ in range(5):
                step = policy.settle(attempts, done=False, error=failure, now=100, kind='laya-sync')
                self.assertEqual((step['ack'], step['count'], step['due']), (False, 0, 130))
                attempts += step['count']
            self.assertEqual(attempts, 7)
        for detail in ('deferred:pending:schema3_ingest_backlog',
                       'deferred:pending:schema3_vector_fill',
                       'deferred:pending:schema3_progress_ingest_extra'):
            failure = policy.DispatchFailure('target_deferred', detail=detail)
            step = policy.settle(7, done=False, error=failure, now=100, kind='laya-sync')
            self.assertEqual((step['ack'], step['count'], step['due']), (False, 1, 400))
        failure = policy.DispatchFailure('target_deferred',
                                         detail='deferred:pending:schema3_progress_ingest')
        self.assertEqual(policy.settle(0, done=False, error=failure, now=100,
                                       kind='laya-compile')['count'], 1)

    def test_fixed_kinds_and_priority_classes(self):
        self.assertEqual(policy.KINDS, {'laya-sync': 2, 'laya-compile': 2,
                                        'learning': 3, 'auto': 3})

    def test_validate_accepts_only_fixed_shapes(self):
        for kind, target in (('learning', '/synthetic/a'),
                             ('auto', '/synthetic/a'),
                             ('laya-sync', '/synthetic/a'),
                             ('laya-compile', '/synthetic/a')):
            policy.validate_wake(kind, target)
        bad = (('unknown', ''), ('learning', 'relative'), ('learning', ''),
               ('auto', '/' + 'a' * 4096))
        for kind, target in bad:
            with self.assertRaises(ValueError, msg=(kind, target)) as caught:
                policy.validate_wake(kind, target)
            self.assertEqual(str(caught.exception), 'invalid_runtime_wake')

    def test_key_coalesces_by_kind_and_target(self):
        self.assertEqual(policy.wake_key('learning', '/a'), policy.wake_key('learning', '/a'))
        self.assertNotEqual(policy.wake_key('learning', '/a'), policy.wake_key('auto', '/a'))
        self.assertEqual(len(policy.wake_key('index', '')), 64)

    def test_capacity_reserves_one_slot_for_index(self):
        for kind in ('laya-sync', 'laya-compile', 'learning', 'auto'):
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

    def test_next_row_orders_by_kind_class_then_due_then_id(self):
        same = [{'kind': 'auto', 'due': 2, 'id': 'a'}, {'kind': 'learning', 'due': 1, 'id': 'b'},
                {'kind': 'learning', 'due': 1, 'id': 'a'}]
        self.assertEqual(policy.next_row(same), {'kind': 'learning', 'due': 1, 'id': 'a'})

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
        deferred = policy.DispatchFailure('target_deferred')
        self.assertEqual(policy.settle(0, done=False, error=deferred, now=10),
                         {'ack': False, 'due': 15, 'count': 1,
                          'reason': 'dispatch:target_deferred'})
        with self.assertRaises(ValueError):
            policy.DispatchFailure('private/path')

    def test_laya_retry_cap_drops_instead_of_retrying_forever(self):
        cap = policy.RETRY_CAP_ATTEMPTS
        deferred = policy.DispatchFailure('target_deferred')
        for kind in ('laya-sync', 'laya-compile'):
            below = policy.settle(cap - 2, done=False, error=deferred, now=10, kind=kind)
            self.assertEqual((below['ack'], below.get('drop')), (False, None))
            at = policy.settle(cap - 1, done=False, error=deferred, now=10, kind=kind)
            self.assertEqual((at['ack'], at['drop'], at['count']), (True, 'retry_cap', 1))
            self.assertEqual(at['reason'], policy.failure_reason(deferred))
        # Waiting on a starting owner is not a failure and never reaches the cap.
        preparing = policy.DispatchFailure('target_preparing')
        self.assertFalse(policy.settle(cap * 3, done=False, error=preparing, now=10,
                                       kind='laya-sync')['ack'])
        # Other kinds keep their own retry semantics.
        for kind in ('learning', 'auto', None):
            self.assertFalse(policy.settle(cap * 3, done=False, now=10, kind=kind)['ack'])

    def test_a_missing_target_backs_off_until_it_stayed_missing_for_a_day(self):
        missing = policy.TargetMissing()
        day = policy.MISSING_DROP_SECS
        need = policy.MISSING_DROP_MIN_OBSERVATIONS
        # Too short a streak, or too few observations: back off without
        # counting an attempt.
        for first, seen in ((10, 1), (10 - day, 1), (10 - day, need - 1), (10 - day + 1, 500)):
            step = policy.settle(500, done=False, error=missing, now=10, kind='laya-sync',
                                 missing=(first, seen))
            self.assertEqual((step['ack'], step['reason'], step['count']),
                             (False, 'target_missing', 0), (first, seen))
            self.assertEqual(step['due'], 10 + policy.retry_delay(seen - 1))
        step = policy.settle(0, done=False, error=missing, now=10, kind='laya-compile',
                             missing=(10 - day, need))
        self.assertEqual((step['ack'], step['drop'], step['count']), (True, 'target_missing', 0))
        # A row already at the generic cap is not dropped by a missing target.
        self.assertFalse(policy.settle(policy.RETRY_CAP_ATTEMPTS * 2, done=False, error=missing,
                                       now=10, kind='laya-sync', missing=(10, 1))['ack'])

    def test_dropped_answer_acks_as_a_recorded_drop(self):
        self.assertEqual(policy.settle(7, done=policy.Dropped('target_missing'), now=10),
                         {'ack': True, 'due': 10, 'reason': None, 'count': 0,
                          'drop': 'target_missing'})
        self.assertTrue(policy.Dropped('retry_cap'))
        with self.assertRaises(ValueError):
            policy.Dropped('/private/path')

    def test_any_due_ignores_backed_off_rows(self):
        rows = [{'kind': 'learning', 'due': 20}, {'kind': 'laya-sync', 'due': 30}]
        self.assertFalse(policy.any_due(rows, 19.9))
        self.assertTrue(policy.any_due(rows, 20))
        self.assertFalse(policy.any_due([], 100))
