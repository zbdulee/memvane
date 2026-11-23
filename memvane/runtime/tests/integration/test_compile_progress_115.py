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

    def test_an_uncounted_outcome_in_between_does_not_start_the_streak_over(self):
        # W1c r2 review F-9: a backlog that is put back every pass, with the compile run lock
        # held by another process once in a while, still reaches the cap.
        self.queue.put('laya-compile', self.target, now=0)

        def busy(row):
            raise policy.DispatchFailure('target_deferred', detail='deferred:deferred:owner_busy')
        wall = 1000
        for n in range(1, 7):
            self.run_once(self.progress, wall)
            wall = self.queue.pending()[0]['due']
            if n % 2 == 0:
                self.run_once(busy, wall)
                wall = self.queue.pending()[0]['due']
        self.assertEqual((self.streak(), self.queue.pending()[0]['attempts']), ([6], 0))

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


class ShrinkingBacklog(ProductiveCompileWake):
    """The streak counts only passes that did not leave less than any pass before them."""

    def passes(self, backlogs, wall=1000):
        """Run one pass per backlog value; the rows after each and the drops."""
        drops, rows = [], []
        for backlog in backlogs:
            def execute(row, backlog=backlog):
                error = policy.DispatchFailure('target_deferred', detail='deferred:pending:compile_progress')
                error.backlog = backlog
                raise error
            self.run_once(execute, wall, drops)
            row = self.queue.pending()[0]
            rows.append((row['attempts'], row['due'] - wall))
            wall = row['due']
        return rows, drops

    def test_a_backlog_of_three_thousand_rows_that_keeps_shrinking_is_never_counted(self):
        self.queue.put('laya-compile', self.target, now=0)
        rows, drops = self.passes(range(2990, -1, -10))
        self.assertEqual(len(rows), 300)
        self.assertEqual(set(rows), {(0, 30)})
        self.assertEqual((drops, self.streak()), ([], [0]))

    def test_a_backlog_that_is_put_back_reaches_the_cap(self):
        self.queue.put('laya-compile', self.target, now=0)
        cap = policy.COMPILE_PROGRESS_STREAK_CAP
        # One new low, then the same amount comes back every pass.
        rows, _drops = self.passes([50] + [60, 55] * (cap // 2))
        self.assertEqual(set(rows), {(0, 30)})
        self.assertEqual(self.streak(), [cap])
        rows, _drops = self.passes([60], wall=10 ** 6)
        self.assertEqual(rows, [(1, policy.retry_delay(0))])
        # A new low after that starts the free streak over; the counted attempt stays.
        rows, _drops = self.passes([49], wall=2 * 10 ** 6)
        self.assertEqual((rows, self.streak()), ([(1, 30)], [0]))

    def test_a_pass_without_a_backlog_or_with_an_odd_one_counts(self):
        self.queue.put('laya-compile', self.target, now=0)
        rows, _drops = self.passes([40, None, 'many', -1, True, 40, 41])
        self.assertEqual(set(rows), {(0, 30)})
        self.assertEqual(self.streak(), [6])
        self.passes([39], wall=10 ** 6)
        self.assertEqual(self.streak(), [0])
