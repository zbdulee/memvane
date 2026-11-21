"""A productive compile continuation preserves the counted-failure budget."""
from memvane.runtime import runtime_wake_policy as policy
from memvane.runtime import runtime_work as runtime
from tests.support.case import FixtureCase


class ProductiveCompileWake(FixtureCase):
    def setUp(self):
        super().setUp()
        self.queue = runtime.WakeQueue(self.runtime)
        self.target = str(self.tmp / 'project')

    def test_progress_remains_queued_at_retry_cap_across_multiple_passes(self):
        self.queue.put('laya-compile', self.target, now=0)
        with self.queue.connect() as db:
            db.execute('UPDATE wakes SET attempts=143')
        drops = []

        def execute(row):
            raise policy.DispatchFailure('target_deferred',
                                         detail='deferred:pending:compile_progress')

        for n in range(4):
            runtime.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                          wall=lambda: 1000 + n * 500, sleep=lambda _: None, max_runs=1,
                          on_drop=lambda *args, **kwargs: drops.append(kwargs))
            rows = self.queue.pending()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['attempts'], 143)
        self.assertEqual(drops, [])

    def progress(self, row):
        raise policy.DispatchFailure('target_deferred', detail='deferred:pending:compile_progress')

    def run_once(self, execute, wall, drops=None):
        return runtime.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                             wall=lambda: wall, sleep=lambda _: None, max_runs=1,
                             on_drop=lambda *args, **kwargs: (drops if drops is not None else []).append(kwargs))

    def streak(self):
        path = self.queue.root / runtime.PROGRESS_FILE
        rows = runtime._read_map(path) if path.exists() else {}
        return [row['seen'] for row in rows.values()]

    def test_progress_passes_in_a_row_stop_being_free_past_the_streak_cap(self):
        self.queue.put('laya-compile', self.target, now=0)
        cap = policy.COMPILE_PROGRESS_STREAK_CAP
        wall = 1000
        for n in range(1, cap + 1):
            self.run_once(self.progress, wall)
            row = self.queue.pending()[0]
            self.assertEqual((row['attempts'], row['due']), (0, wall + 30), n)
            wall = row['due']
        self.assertEqual(self.streak(), [cap])
        # The next one is counted like any failure: an attempt and the failure backoff.
        self.run_once(self.progress, wall)
        row = self.queue.pending()[0]
        self.assertEqual((row['attempts'], row['due']), (1, wall + policy.retry_delay(0)))
        self.assertEqual(self.streak(), [cap + 1])

    def test_a_wake_that_only_ever_reports_progress_is_dropped_at_the_retry_cap(self):
        self.queue.put('laya-compile', self.target, now=0)
        drops, wall, runs = [], 1000, 0
        while self.queue.pending():
            self.run_once(self.progress, wall, drops)
            runs += 1
            rows = self.queue.pending()
            wall = rows[0]['due'] if rows else wall
            self.assertLess(runs, 1000)
        self.assertEqual(runs, policy.COMPILE_PROGRESS_STREAK_CAP + policy.RETRY_CAP_ATTEMPTS)
        self.assertEqual([row['reason'] for row in drops], ['retry_cap'])
        self.assertEqual(self.streak(), [])

    def test_another_outcome_starts_the_progress_streak_over(self):
        self.queue.put('laya-compile', self.target, now=0)
        for n in range(3):
            self.run_once(self.progress, 1000 + n * 100)
        self.assertEqual(self.streak(), [3])

        def unlabeled(row):
            raise policy.DispatchFailure('target_deferred', detail='deferred:pending:unlabeled')
        self.run_once(unlabeled, 2000)
        self.assertEqual((self.streak(), self.queue.pending()[0]['attempts']), ([], 1))
        self.run_once(self.progress, 3000)
        self.assertEqual((self.streak(), self.queue.pending()[0]['attempts']), ([1], 1))
        # A finished pass clears it with the wake.
        self.run_once(lambda row: True, 4000)
        self.assertEqual((self.streak(), self.queue.pending()), ([], []))

    def test_the_streak_rule_is_the_policy_alone(self):
        error = policy.DispatchFailure('target_deferred', detail='deferred:pending:compile_progress')
        cap = policy.COMPILE_PROGRESS_STREAK_CAP
        for seen, count in ((None, 0), (1, 0), (cap, 0), (cap + 1, 1), (cap + 50, 1)):
            with self.subTest(seen=seen):
                step = policy.settle(0, done=False, now=1000, kind='laya-compile', error=error, progress=seen)
                self.assertEqual((step['count'], step['ack']), (count, False))
        # At the retry cap a counted progress pass drops the wake.
        step = policy.settle(policy.RETRY_CAP_ATTEMPTS - 1, done=False, now=1000, kind='laya-compile',
                             error=error, progress=cap + 1)
        self.assertEqual(step.get('drop'), 'retry_cap')
        # Another kind never takes the progress rule, whatever the count says.
        step = policy.settle(0, done=False, now=1000, kind='laya-sync', error=error, progress=1)
        self.assertEqual(step['count'], 1)

    def test_unproductive_pending_still_exhausts_retry_cap(self):
        self.queue.put('laya-compile', self.target, now=0)
        with self.queue.connect() as db:
            db.execute('UPDATE wakes SET attempts=143')
        drops = []

        def execute(row):
            raise policy.DispatchFailure('target_deferred',
                                         detail='deferred:pending:unlabeled')

        runtime.drain(self.queue, execute=execute, ready=lambda: True, clock=lambda: 0,
                      wall=lambda: 1000, sleep=lambda _: None, max_runs=1,
                      on_drop=lambda *args, **kwargs: drops.append(kwargs))
        self.assertEqual(self.queue.pending(), [])
        self.assertEqual([row['reason'] for row in drops], ['retry_cap'])

    def test_compile_progress_detail_is_exact_and_kind_scoped(self):
        for kind, detail in [('laya-sync', 'deferred:pending:compile_progress'),
                             ('laya-compile', 'deferred:pending:compile_progress:extra'),
                             ('laya-compile', 'deferred:deferred:compile_progress')]:
            with self.subTest(kind=kind, detail=detail):
                error = policy.DispatchFailure('target_deferred', detail=detail)
                step = policy.settle(143, done=False, now=1000, kind=kind, error=error)
                self.assertEqual(step.get('drop'), 'retry_cap')
