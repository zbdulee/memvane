"""A teacher pass and a training pass stopped after each durable write, then entered again.

For every write k of one pass over one queued case, a child interpreter runs
the real pass and is killed right after write k (``tests/support/crash.py``).
The namespace it leaves must satisfy the invariants, and passes entered again
must end the case with the host CLI started at most once for it. The number
of writes of a pass is fixed here: a new write is a new crash point. The
point right after the host CLI returned its answer is one of them.

The training pass runs in an enrolled install (``CycleFixture``), which a new
interpreter cannot rebuild: there the stop is ``crash.interrupt_after``. Its
points include the trainer having written the candidate artifact.
"""
from __future__ import annotations

import shutil

from memvane.learning.context_learning import shared, teacher_state
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture
from memvane.learning.tests.statemachine.test_m04_cycle import CycleFixture, ProcessEnded
from memvane.learning.tests.lp1 import Lp1Fixture
from memvane.learning.tests.support import crash, invariants

HOST = 'claude'
RID = 'case'
# scenario -> (fake CLI behaviour, durable writes of one uninterrupted pass,
#              case states the re-entered passes may end in).
SCENARIOS = {
    'answered': ({}, 9, ('gold', 'pending_review')),
    'provider_refused': ({'mode': 'quota'}, 7, ('queued', 'pending_review')),
    'content_refused': ({'mode': 'content'}, 6, ('pending_review',)),
    'cli_failed': ({'mode': 'exit'}, 7, ('pending_review',)),
}
# The scenarios in which a call may be made again: the provider proved nothing was answered.
MAY_CALL_AGAIN = {'provider_refused'}
REENTRIES = 3


class CrashPoints(TeacherFixture):
    def fresh(self, behaviour):
        for name in (shared.DATABASE_NAME, shared.DATABASE_NAME + '-wal',
                     shared.DATABASE_NAME + '-shm', teacher_state.STATE_NAME):
            path = self.root / name
            if path.exists():
                path.unlink()
        calls = self.logs / 'calls.jsonl'
        if calls.exists():
            calls.unlink()
        self.add(RID)
        self.scenario(HOST, behaviour)

    def status(self):
        return self.cases()[RID][0]

    def test_writes_of_one_pass_are_counted(self):
        for name, (behaviour, writes, _ends) in SCENARIOS.items():
            with self.subTest(scenario=name):
                self.fresh(behaviour)
                self.assertEqual(crash.count(self.run_pass), writes)

    def test_a_pass_killed_after_any_write_leaves_a_state_the_next_passes_finish(self):
        points = 0
        for name, (behaviour, writes, ends) in SCENARIOS.items():
            for k in range(1, writes + 1):
                with self.subTest(scenario=name, k=k):
                    self.fresh(behaviour)
                    self.assertEqual(crash.crash_after(k, self), crash.KILLED)
                    self.assertEqual(invariants.check(self.root), [])
                    for _ in range(REENTRIES):
                        self.run_pass()
                        self.assertEqual(invariants.check(self.root), [])
                    self.assertIn(self.status(), ends)
                    if name not in MAY_CALL_AGAIN:
                        # G-1: the host was asked about this case once at most.
                        self.assertLessEqual(self.runs(HOST), 1)
                points += 1
            # One write past the last: the pass ends by itself.
            self.fresh(behaviour)
            self.assertEqual(crash.crash_after(writes + 1, self), crash.FINISHED)
        self.assertEqual(points, sum(writes for _b, writes, _e in SCENARIOS.values()))


# Durable writes of one uninterrupted training pass over 100 gold cases, in a
# namespace an earlier pass already ran in (the first pass of a namespace also
# writes what is written once).
TRAINING_WRITES = 10


class TrainingCrashPoints(CycleFixture):
    def fresh(self):
        with shared._db(self.root) as db:
            for table in ('cycles', 'learning_jobs', 'learning_consumption', 'learning_evaluations'):
                if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                    db.execute('DELETE FROM %s' % table)
            db.execute('UPDATE cases SET trained_cycle=NULL')
        cycles = self.root / 'cycles'
        if cycles.is_dir():
            import shutil
            shutil.rmtree(cycles)
        self.trained.clear()
        self.script[:] = []

    def finish(self):
        """Passes entered again until the job is staged; how many it took."""
        for entered in range(1, REENTRIES + 1):
            self.due_now()
            self.worker()
            self.assertEqual(self.violations(), [])
            if self.job() and self.job()[1] == 'staged':
                return entered
        self.fail('not staged after %d passes: %r %r' % (REENTRIES, self.job(), self.cycle()))

    def setUp(self):
        super().setUp()
        self.fresh()
        self.worker()

    def test_writes_of_one_training_pass_are_counted(self):
        self.fresh()
        self.assertEqual(crash.count(self.worker), TRAINING_WRITES)
        self.assertEqual((self.job()[1], self.cycle()), ('staged', 'staged'))

    def test_a_training_pass_stopped_after_any_write_is_finished_by_the_next_passes(self):
        for k in range(1, TRAINING_WRITES + 1):
            with self.subTest(k=k):
                self.fresh()
                self.assertTrue(crash.interrupt_after(k, self.worker))
                self.assertEqual(self.violations(), [])
                self.finish()
                self.assertEqual((self.job()[1], self.cycle()), ('staged', 'staged'))
                self.assertEqual(self.marks(), {'marked': 100})
                # One training, or two when the first ended before its completion was recorded.
                self.assertIn(self.trained, ([100], [100, 100]))
        self.fresh()
        self.assertFalse(crash.interrupt_after(TRAINING_WRITES + 1, self.worker))

    def test_a_process_gone_right_after_the_trainer_wrote_the_artifact(self):
        # The external effect between two writes: the artifact exists, nothing records it.
        self.fresh()
        self.assertEqual(self.attempt('artifact_then_end')['status'], 'process_ended')
        self.assertEqual(self.violations(), [])
        self.assertEqual(self.finish(), 1)
        self.assertEqual((self.trained, self.marks()), ([100, 100], {'marked': 100}))
        folder = self.root / 'cycles' / self.job()[0]
        self.assertEqual(len([path for path in folder.iterdir() if '.uncertain-' in path.name]), 1)


class WorkerProcessCrashPoints(Lp1Fixture):
    """The worker command as a real child process, killed after each durable write.

    The namespace a pass starts from is kept as a copy and restored for every
    point. Two passes: the one that trains 100 gold cases, and the one that
    evaluates the staged cycle against 50 later cases and applies it (its
    points include the journal, pointer, receipt and binding writes on either
    side of the resident's answers).
    """
    # pass -> durable writes of one uninterrupted run of it.
    WRITES = {'training': 13, 'application': 8}

    def keep(self):
        self.kept = self.tmp / 'namespace-copy'
        if self.kept.exists():
            shutil.rmtree(self.kept)
        shutil.copytree(self.root, self.kept, symlinks=True)

    def restore(self):
        for path in self.root.iterdir():
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        shutil.copytree(self.kept, self.root, symlinks=True, dirs_exist_ok=True)

    def violations(self):
        # The gold cases are seeded as rows, without the teacher call a ledger row records.
        return [line for line in invariants.check(self.root) if not line.startswith('I-3 ')]

    def statuses(self):
        counts = self.counts()
        return counts['jobs'], counts['cycles']

    def prepare(self, name):
        self.seed_gold('t', 100, empty=20)
        if name == 'application':
            code, result = self.worker_child(seeded=True)
            self.assertEqual((code, result['cycle']['status']), (0, 'staged_waiting_quality_cohort'), result)
            self.seed_gold('v', 50, empty=10)
        self.keep()

    def run_pass(self, name, k=None):
        self.restore()
        return self.worker_child(k, seeded=True)

    def finished(self, name):
        """The durable end of the pass, whatever a later pass went on to do."""
        jobs, cycles = self.statuses()
        if name == 'training':
            return jobs.get('staged', 0) >= 1 and sum(cycles.values()) >= 1
        return cycles.get('applied') == 1 and self.pointer()['cycle_id'] == self.jobs()[0]

    def check(self, name, ended):
        self.prepare(name)
        k = 0
        while True:
            k += 1
            self.assertLess(k, 80, 'the pass never finished')
            code, result = self.run_pass(name, k)
            if code == 0:
                break
            with self.subTest(pass_name=name, k=k):
                self.assertEqual((code, result), (crash.KILLED, None))
                self.assertEqual(self.violations(), [])
                for _ in range(REENTRIES):
                    code, result = self.worker_child(seeded=True)
                    self.assertEqual(code, 0, result)
                    self.assertEqual(self.violations(), [])
                    if self.finished(name):
                        break
                self.assertTrue(self.finished(name), result)
        # One write past the last: the pass ends by itself. The count is fixed here:
        # a new write is a new point to look at.
        self.assertEqual(result['cycle']['status'], ended, result)
        self.assertTrue(self.finished(name))
        self.assertEqual(k - 1, self.WRITES[name])

    def test_the_training_pass_killed_after_any_write_is_finished_by_later_passes(self):
        self.check('training', 'staged_waiting_quality_cohort')
        self.assertEqual(self.statuses(), ({'staged': 1}, {'staged': 1}))

    def test_the_applying_pass_killed_after_any_write_is_finished_by_later_passes(self):
        self.check('application', 'applied')
        self.assertEqual(self.statuses()[1], {'applied': 1})
