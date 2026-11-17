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
