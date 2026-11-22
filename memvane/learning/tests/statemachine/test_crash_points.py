"""A teacher pass stopped after each of its durable writes, then entered again.

For every write k of one pass over one queued case, a child interpreter runs
the real pass and is killed right after write k (``tests/support/crash.py``).
The namespace it leaves must satisfy the invariants, and passes entered again
must end the case with the host CLI started at most once for it. The number
of writes of a pass is fixed here: a new write is a new crash point.
"""
from __future__ import annotations

from memvane.learning.context_learning import shared, teacher_state
from memvane.learning.tests.fake_teacher_cli_121 import TeacherFixture
from memvane.learning.tests.support import crash, invariants

HOST = 'claude'
RID = 'case'
# scenario -> (fake CLI behaviour, durable writes of one uninterrupted pass,
#              case states the re-entered passes may end in).
SCENARIOS = {
    'answered': ({}, 8, ('gold', 'pending_review')),
    'provider_refused': ({'mode': 'quota'}, 7, ('queued', 'pending_review')),
}
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
                    if name == 'answered':
                        # G-1: an answer may have been produced once, never twice.
                        self.assertLessEqual(self.runs(HOST), 1)
                points += 1
            # One write past the last: the pass ends by itself.
            self.fresh(behaviour)
            self.assertEqual(crash.crash_after(writes + 1, self), crash.FINISHED)
        self.assertEqual(points, sum(writes for _b, writes, _e in SCENARIOS.values()))
