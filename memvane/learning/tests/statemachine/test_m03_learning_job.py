"""M3 (learning job): every job state under every event of the queue.

Each event is driven through the real ``LearningQueue`` writer. The expected
state is the model's ``next``: a pair the model does not list must leave the
status as it was. ``source_revoked`` is written by the training step itself
and is driven by the cycle tests; here it is the one event not driven.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memvane.learning.context_learning import learning_queue
from memvane.learning.context_learning.learning_queue import LearningQueue
from memvane.learning.context_learning.state_model import ABSENT, M3

NOW = 1_000_000.0
NOT_DRIVEN = {'source_revoked'}


class LearningJobTable(unittest.TestCase):
    def fresh(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)

        @contextmanager
        def connect():
            db = sqlite3.connect(self.root / 'fixture.sqlite3')
            try:
                with db:
                    yield db
            finally:
                db.close()
        self.q = LearningQueue(self.root, connect=connect)
        self.job_id = None

    def row(self, i):
        sample = {'prompt': {'text': 'question %d' % i},
                  'candidates': [{'candidate_id': 'card', 'revision_sha256': str(i)}],
                  'request_id': 'r%d' % i}
        return ('owner', 'r%d' % i, 'input%d' % i, json.dumps(sample), json.dumps({'card': 'body %d' % i}),
                json.dumps({'selected_ids': ['card']}), 'train', '/synthetic/owner', 'corpus', '[]',
                None, 100, 'source')

    def plan(self, rows=(1, 2)):
        return self.q.plan([self.row(i) for i in rows], approval='a' * 64, cohort='c', family='f' * 64,
                           minimum=2, maximum=4)

    def status(self):
        if self.job_id is None:
            return ABSENT
        return self.q.get(self.job_id)['status']

    def force(self, **fields):
        with self.q.connect() as db:
            for name, value in fields.items():
                db.execute('UPDATE learning_jobs SET %s=? WHERE job_id=?' % name, (value, self.job_id))

    def hold(self, now=NOW):
        """Enter a claim and keep it: the attempt that owns the job has not returned."""
        self.held = self.q.claim(self.job_id, now=now)
        self.addCleanup(self.release)
        return self.held.__enter__()

    def release(self):
        held, self.held = self.held, None
        if held is not None:
            held.__exit__(None, None, None)

    def build(self, state):
        """Reach ``state`` with the real writers only."""
        self.fresh()
        self.held = None
        if state == ABSENT:
            return
        self.job_id = self.plan()['id']
        if state == 'running':
            self.assertIsNotNone(self.hold())
        elif state == 'deferred':
            self.q.defer(self.job_id, 'resource', now=NOW - 1000)
        elif state == 'staged':
            self.q.complete(self.job_id)
        elif state == 'blocked':
            for _ in range(3):
                self.q.defer(self.job_id, learning_queue.REARMED_REASON, failure=True,
                             now=NOW - learning_queue.REARM_SECONDS)

    def drive(self, state, event):
        if event == 'plan':
            job = self.plan()
            if job is not None:
                self.job_id = job['id']
        elif self.job_id is None:
            # No row: every other event names a job that does not exist.
            self.assertIsNone(self.q.get('cohort-missing'))
            self.assertEqual(self.q.rearm(now=NOW), [])
        elif event == 'claim':
            if state == 'running':
                # The running attempt holds the claim lock: a second claim gets nothing.
                with self.q.claim(self.job_id, now=NOW) as second:
                    self.assertIsNone(second)
            else:
                self.assertEqual(self.hold() is not None, state in ('queued', 'deferred'))
        elif event == 'claim_released':
            self.release()
        elif event == 'complete':
            try:
                self.q.complete(self.job_id)
            except ValueError as exc:
                self.assertEqual(str(exc), 'learning_job_terminal')
        elif event == 'defer':
            self.q.defer(self.job_id, 'resource', now=NOW)
        elif event == 'fail':
            self.q.defer(self.job_id, 'trainer_failed', failure=True, now=NOW)
        elif event == 'fail_limit':
            if state in ('queued', 'running', 'deferred'):
                self.force(failures=2)
            self.q.defer(self.job_id, 'trainer_failed', failure=True, now=NOW)
        elif event == 'consumed_elsewhere':
            job = self.q.get(self.job_id)
            with self.q.connect() as db:
                db.execute('INSERT OR IGNORE INTO learning_consumption VALUES (?,?,?)',
                           (self.q.scope, job['members'][0]['key'], 'another-job'))
            if state == 'running':
                # The owner is gone without leaving its block: the lock is free, the row says running.
                self.release()
                self.force(status='running', retry_at=0)
            with self.q.claim(self.job_id, now=NOW) as claim:
                self.assertIsNone(claim)
        elif event == 'rearm':
            self.q.rearm(now=NOW)

    def test_every_job_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        events = [event for event in M3.events if event not in NOT_DRIVEN]
        for state in M3.states:
            for event in events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.status(), state)
                    self.drive(state, event)
                    self.assertEqual(self.status(), M3.next(state, event))
                cells += 1
                if (state, event) in M3.transitions:
                    driven.add((state, event))
        self.assertEqual(cells, len(M3.states) * len(events))
        self.assertEqual((len(M3.states), len(events)), (6, 9))
        self.assertEqual(driven, {pair for pair in M3.transitions if pair[1] not in NOT_DRIVEN})
        self.assertEqual(set(M3.events) - set(events), NOT_DRIVEN)

    def blocked(self, reason, *, at):
        self.fresh()
        self.job_id = self.plan()['id']
        if reason in learning_queue.FINAL_BLOCKS:
            self.force(status='blocked', reason=reason, retry_at=at)
        else:
            for _ in range(3):
                self.q.defer(self.job_id, reason, failure=True, now=at)

    def test_a_job_blocked_by_trainer_failures_is_queued_again_a_day_later(self):
        self.blocked('trainer_failed', at=NOW)
        job = self.q.get(self.job_id)
        self.assertEqual((job['status'], job['failures'], job['retry_at']),
                         ('blocked', 3, NOW + learning_queue.REARM_SECONDS))
        self.assertEqual(self.q.rearm(now=NOW + learning_queue.REARM_SECONDS - 1), [])
        self.assertEqual(self.status(), 'blocked')
        # Blocked and not yet due: the plan answers with the same job.
        self.assertEqual(self.plan()['id'], self.job_id)
        self.assertEqual(self.q.rearm(now=NOW + learning_queue.REARM_SECONDS), [self.job_id])
        after = self.q.get(self.job_id)
        self.assertEqual((after['status'], after['failures'], after['attempts'], after['retry_at'],
                          after['reason']), ('queued', 0, 0, 0, None))
        self.assertEqual(after['members'], job['members'])
        self.assertEqual(self.q.rearm(now=NOW + 10 * learning_queue.REARM_SECONDS), [])
        with self.q.claim(self.job_id, now=NOW + learning_queue.REARM_SECONDS) as claim:
            self.assertIsNotNone(claim)
            self.q.complete(self.job_id)
        self.assertEqual(self.status(), 'staged')

    def test_a_job_blocked_before_the_rearm_time_was_recorded_is_due_at_once(self):
        # An earlier version left a short backoff in retry_at when it blocked a job.
        self.blocked('trainer_failed', at=NOW)
        self.force(retry_at=NOW + 300)
        self.assertEqual(self.q.rearm(now=NOW + 301), [self.job_id])

    def test_a_block_the_members_caused_stays_and_the_next_plan_pins_other_members(self):
        for reason in learning_queue.FINAL_BLOCKS:
            with self.subTest(reason=reason):
                self.blocked(reason, at=0)
                self.assertEqual(self.q.rearm(now=NOW * 10), [])
                self.assertEqual(self.status(), 'blocked')
                # The same members: their digest is the same id, and the job stays blocked.
                same = self.plan()
                self.assertEqual((same['id'], same['status'], same['reason']), (self.job_id, 'blocked', reason))
                self.assertIsNone(self.plan((1,)))
                later = self.plan((3, 4))
                self.assertNotEqual(later['id'], self.job_id)
                self.assertEqual(later['status'], 'queued')
                self.assertEqual(self.status(), 'blocked')

    def test_a_block_of_another_reason_is_not_rearmed(self):
        self.blocked('failed', at=0)
        self.assertEqual(self.q.rearm(now=NOW * 10), [])
        self.assertEqual(self.plan()['id'], self.job_id)


if __name__ == '__main__':
    unittest.main()
