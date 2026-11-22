"""M5 (evaluation): every evaluation state under every event of its queue.

Each event is driven through the real ``EvaluationQueue`` writer; the expected
state is the model's ``next``. ``carried`` (the row deleted when a candidate is
carried to a renewed approval) is written by the carry itself and is driven by
``memvane/learning/tests/integration/test_carry_122.py``; here it is the one
event not driven.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memvane.learning.context_learning.evaluation_queue import EvaluationQueue
from memvane.learning.context_learning.state_model import ABSENT, M5

NOW = 1_000_000.0
CYCLE = 'cohort-' + 'a' * 20
NOT_DRIVEN = {'carried'}


class EvaluationTable(unittest.TestCase):
    held = None

    def fresh(self):
        self.release()  # A claim of the previous cell ends in its own database.
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
        self.q = EvaluationQueue(self.root, connect=connect)
        self.job_id, self.held = None, None

    def row(self):
        return self.q.get(self.job_id) if self.job_id else None

    def status(self):
        row = self.row()
        return row['status'] if row else ABSENT

    def hold(self):
        self.held = self.q.claim(self.job_id, now=NOW)
        self.addCleanup(self.release)
        return self.held.__enter__()

    def release(self):
        held, self.held = self.held, None
        if held is not None:
            held.__exit__(None, None, None)

    def force(self, **fields):
        with self.q.connect() as db:
            for name, value in fields.items():
                db.execute('UPDATE learning_evaluations SET %s=? WHERE job_id=?' % name, (value, self.job_id))

    def build(self, state):
        self.fresh()
        if state == ABSENT:
            return
        self.job_id = self.q.ensure(CYCLE, 'stage', 'validation')['id']
        if state == 'running':
            self.assertIsNotNone(self.hold())
        elif state == 'deferred':
            self.q.defer(self.job_id, 'resource', now=NOW - 1000)
        elif state == 'waiting_quality':
            self.q.finish(self.job_id, 'waiting_quality')
        elif state == 'completed':
            self.q.finish(self.job_id, 'completed')
        elif state == 'blocked':
            self.q.finish(self.job_id, 'blocked', 'staged_source_revoked')

    def drive(self, state, event):
        if event == 'ensure':
            self.job_id = self.q.ensure(CYCLE, 'stage', 'validation')['id']
        elif event == 'reject':
            # Closing a candidate that was never evaluated records the closure.
            self.job_id = self.q.reject(CYCLE, 'apply_quality_regression')['id']
        elif self.job_id is None:
            self.assertIsNone(self.q.get('evaluate:' + CYCLE))
            self.assertEqual(self.q.pending(now=NOW), [])
        elif event == 'validation_changed':
            self.q.ensure(CYCLE, 'stage', 'other-validation')
        elif event == 'claim':
            if state == 'running':
                with self.q.claim(self.job_id, now=NOW) as second:
                    self.assertIsNone(second)
            else:
                self.assertEqual(self.hold() is not None, state in ('queued', 'deferred'))
        elif event == 'claim_released':
            self.release()
        elif event == 'evaluated':
            self.q.finish(self.job_id, 'completed')
        elif event == 'too_few_cases':
            self.q.finish(self.job_id, 'waiting_quality')
        elif event == 'defer':
            self.q.defer(self.job_id, 'resource', now=NOW)
        elif event == 'fail_limit':
            if state in ('queued', 'running', 'deferred'):
                self.force(failures=2)
            self.q.defer(self.job_id, 'evaluation_failed', failure=True, now=NOW)
        elif event == 'refresh':
            self.q.refresh(CYCLE)
        elif event == 'apply_failure_limit':
            self.q.apply_failed(self.job_id, 'apply_prepared_inference_required', 1)
        elif event == 'block':
            self.q.finish(self.job_id, 'blocked', 'staged_source_revoked')

    def test_every_evaluation_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        events = [event for event in M5.events if event not in NOT_DRIVEN]
        for state in M5.states:
            for event in events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.status(), state)
                    self.drive(state, event)
                    self.assertEqual(self.status(), M5.next(state, event))
                    # I-8: a blocked evaluation says why.
                    if self.status() == 'blocked':
                        self.assertTrue(self.row()['reason'])
                cells += 1
                if (state, event) in M5.transitions:
                    driven.add((state, event))
        self.assertEqual(cells, len(M5.states) * len(events))
        self.assertEqual((len(M5.states), len(events)), (7, 12))
        self.assertEqual(driven, {pair for pair in M5.transitions if pair[1] not in NOT_DRIVEN})
        self.assertEqual(set(M5.events) - set(events), NOT_DRIVEN)

    def test_an_evaluation_is_never_blocked_without_a_reason(self):
        self.build('queued')
        for reason in (None, ''):
            with self.assertRaisesRegex(ValueError, 'evaluation_block_reason_required'):
                self.q.finish(self.job_id, 'blocked', reason)
        self.assertEqual(self.status(), 'queued')
        self.q.finish(self.job_id, 'completed', 'ignored')
        self.assertEqual((self.status(), self.row()['reason']), ('completed', None))

    def test_failed_applications_are_counted_on_a_completed_evaluation_and_close_it_at_the_limit(self):
        self.build('completed')
        for count in (1, 2):
            row = self.q.apply_failed(self.job_id, 'apply_prepared_inference_required', 3)
            self.assertEqual((row['status'], row['failures'], row['reason']), ('completed', count, None))
        row = self.q.apply_failed(self.job_id, 'apply_prepared_inference_required', 3)
        self.assertEqual((row['status'], row['failures'], row['reason']),
                         ('blocked', 3, 'apply_prepared_inference_required'))

    def test_a_changed_stage_is_refused_and_changes_nothing(self):
        for state in M5.states:
            if state == ABSENT:
                continue
            with self.subTest(state=state):
                self.build(state)
                with self.assertRaisesRegex(ValueError, 'evaluation_stage_changed'):
                    self.q.ensure(CYCLE, 'another-stage', 'validation')
                self.assertEqual(self.status(), state)


if __name__ == '__main__':
    unittest.main()
