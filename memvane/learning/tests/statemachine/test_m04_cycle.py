"""M4 (cycle) and M3 through the worker command: how a training attempt ends.

The install is ``CarryFixture``'s (real enrollment, grant, profile, policy);
every pass is the real worker command. The trainer is a fake whose next
attempt is scripted: it trains, fails, runs out of time with or without moving
the training cursor, leaves a final artifact without a completion record, or
ends the process. The evaluation and apply events of M4 are driven by their
own unit of work; here they are the events not driven.
"""
from __future__ import annotations

import json
import os
from unittest import mock

from memvane.learning.context_learning import automatic_staging, learning_queue, shared
from memvane.learning.context_learning.state_model import ABSENT, M4
from memvane.learning.tests.integration.test_carry_122 import CarryFixture, FakeTrainer

TRAIN_EVENTS = ('train_started', 'trained', 'train_failed', 'train_deferred')
NOT_DRIVEN = {'evaluated', 'reevaluation_needed', 'carried', 'applied'}


class ProcessEnded(BaseException):
    """The worker process ends inside the trainer call: nothing after it runs."""


class ScriptedTrainer(FakeTrainer):
    def call(self, mode, request, result_path, *, cwd):
        if mode != 'train' or not self.case.script:
            return super().call(mode, request, result_path, cwd=cwd)
        step = self.case.script.pop(0)
        self.case.attempts.append(step)
        if step == 'fail':
            raise ValueError('trainer_failed')
        if step == 'timeout':
            raise ValueError('trainer_timeout')
        if step == 'timeout_progress':
            self.case.advance(cwd)
            raise ValueError('trainer_timeout')
        if step == 'defer':
            raise automatic_staging.ResourceDeferred('trainer_failed:learning_yield', {'reason': 'learning_yield'})
        if step == 'artifact_then_fail':
            super().call(mode, request, result_path, cwd=cwd)
            raise ValueError('trainer_failed')
        if step == 'end_process':
            raise ProcessEnded()
        raise AssertionError(step)


class CycleFixture(CarryFixture):
    def setUp(self):
        super().setUp()
        self.script, self.attempts, self.cycle_events, self.groups = [], [], [], 0
        for name, value in (
                ('_trainer', mock.Mock(side_effect=lambda *a, **k: ScriptedTrainer(self))),
                ('_cycle_event', lambda project, outcome, **f: self.cycle_events.append(dict(f, outcome=outcome)))):
            patcher = mock.patch.object(automatic_staging, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.golds('t', 100, empty=20)

    def advance(self, folder):
        """The cursor a trainer commits after one more optimizer group."""
        resume = folder / 'resume'
        resume.mkdir(mode=0o700, exist_ok=True)
        self.groups += 1
        path = resume / 'cursor.json'
        path.write_text(json.dumps({'next_group': self.groups}), encoding='utf-8')
        path.chmod(0o600)

    def attempt(self, *script):
        """One worker pass whose training attempt follows ``script``; the cycle result."""
        self.script[:] = script
        self.due_now()
        try:
            return self.worker()['cycle']
        except ProcessEnded:
            return {'status': 'process_ended'}

    def worker(self):
        """The worker command; a failed cycle is an answer here, not an error."""
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            shared.main(['run-project', str(self.project)])
        return json.loads(out.getvalue())

    def due_now(self):
        with shared._db(self.root) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_jobs'").fetchone():
                db.execute("UPDATE learning_jobs SET retry_at=0 WHERE status IN ('queued','running','deferred')")

    def job(self):
        with shared._db(self.root) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='learning_jobs'").fetchone():
                return None
            rows = db.execute('SELECT job_id,status,failures,reason,retry_at FROM learning_jobs').fetchall()
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def cycle(self):
        rows = self.cycle_rows()
        self.assertLessEqual(len(rows), 1)
        return next(iter(rows.values()), ABSENT)

    def outcomes(self):
        return [event['outcome'] for event in self.cycle_events]


class CycleTable(CycleFixture):
    def build(self, state):
        if state == 'training':
            self.attempt('end_process')
        elif state == 'failed':
            self.attempt('fail')
        elif state == 'deferred':
            self.attempt('defer')
        elif state == 'staged':
            self.attempt()

    def drive(self, event):
        if event == 'train_started':
            # A cycle row begins with the first attempt, however that attempt ends.
            self.attempt('end_process')
        elif event == 'trained':
            self.attempt()
        elif event == 'train_failed':
            self.attempt('fail')
        elif event == 'train_deferred':
            self.attempt('defer')

    def test_every_training_state_under_every_training_event_ends_where_the_model_says(self):
        states = (ABSENT, 'training', 'failed', 'deferred', 'staged')
        cells, driven = 0, set()
        for state in states:
            for event in TRAIN_EVENTS:
                with self.subTest(state=state, event=event):
                    self.reset()
                    self.build(state)
                    self.assertEqual(self.cycle(), state)
                    self.drive(event)
                    expected = M4.next(state, event)
                    if state == ABSENT and event != 'train_started':
                        # The first attempt writes the row and then ends: two events in one pass.
                        expected = M4.next('training', event)
                    self.assertEqual(self.cycle(), expected)
                cells += 1
                if (state, event) in M4.transitions:
                    driven.add((state, event))
        self.assertEqual(cells, len(states) * len(TRAIN_EVENTS))
        self.assertEqual(driven, {pair for pair in M4.transitions if pair[1] not in NOT_DRIVEN})
        self.assertEqual(set(M4.events) - set(TRAIN_EVENTS), NOT_DRIVEN)

    def reset(self):
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
        self.cycle_events.clear()
        self.groups = 0


class TrainingAttempts(CycleFixture):
    def test_three_trainer_failures_block_the_job_and_a_day_later_it_trains_again(self):
        for failures in (1, 2):
            self.assertEqual(self.attempt('fail')['status'], 'failed')
            self.assertEqual(self.job()[1:4], ('deferred', failures, 'trainer_failed'))
        self.assertEqual(self.attempt('fail')['status'], 'failed')
        job_id, status, failures, reason, retry_at = self.job()
        self.assertEqual((status, failures, reason), ('blocked', 3, 'trainer_failed'))
        self.assertEqual(self.cycle(), 'failed')
        # Blocked and not yet due: each pass says so and trains nothing.
        self.assertEqual(self.attempt()['status'], 'learning_job_blocked')
        self.assertEqual((self.trained, self.outcomes()[-1]), ([], 'blocked'))
        self.assertNotIn('rearmed', self.outcomes())
        with shared._db(self.root) as db:
            db.execute('UPDATE learning_jobs SET retry_at=retry_at-?', (learning_queue.REARM_SECONDS,))
        result = self.attempt()
        self.assertEqual(result['cycle_id'], job_id)
        self.assertEqual([e.get('cycle') for e in self.cycle_events if e['outcome'] == 'rearmed'], [job_id])
        self.assertEqual(self.trained, [100])
        self.assertEqual((self.job()[:3], self.cycle()), ((job_id, 'staged', 0), 'staged'))

    def test_a_time_limit_after_the_cursor_moved_is_a_deferral_not_a_failure(self):
        for _ in range(4):
            result = self.attempt('timeout_progress')
            self.assertEqual((result['status'], result['reason']), ('deferred_resource', 'trainer_timeout'))
            self.assertEqual(self.job()[1:4], ('deferred', 0, 'trainer_progress'))
            self.assertEqual(self.cycle(), 'deferred')
        self.assertNotIn('failed', self.outcomes())
        self.attempt()
        self.assertEqual((self.job()[1], self.cycle(), self.trained), ('staged', 'staged', [100]))

    def test_a_time_limit_without_cursor_progress_is_a_failure(self):
        self.assertEqual(self.attempt('timeout')['status'], 'failed')
        self.assertEqual((self.job()[1:4], self.cycle()), (('deferred', 1, 'trainer_failed'), 'failed'))
        # The cursor moved once and then stood still: the second limit is a failure.
        self.attempt('timeout_progress')
        self.assertEqual(self.job()[2], 1)
        self.script[:] = ['timeout']
        self.due_now()
        self.assertEqual(self.worker()['cycle']['status'], 'failed')
        self.assertEqual(self.job()[2], 2)

    def test_a_final_artifact_without_a_completion_record_is_set_aside_and_trained_again(self):
        self.assertEqual(self.attempt('artifact_then_fail')['status'], 'failed')
        job_id = self.job()[0]
        folder = self.root / 'cycles' / job_id
        left = (folder / 'candidate.artifact').read_bytes()
        self.attempt()
        self.assertEqual((self.job()[1:3], self.cycle()), (('staged', 1), 'staged'))
        aside = sorted(path.name for path in folder.iterdir() if '.uncertain-' in path.name)
        self.assertEqual(len(aside), 1)
        self.assertEqual((folder / aside[0]).read_bytes(), left)
        self.assertEqual(self.trained, [100, 100])
        self.assertNotIn('learning_completion_uncertain',
                         [event.get('reason') for event in self.cycle_events])

    def test_a_process_that_ends_while_training_leaves_a_job_the_next_pass_trains(self):
        self.assertEqual(self.attempt('end_process')['status'], 'process_ended')
        self.assertEqual((self.job()[1:4], self.cycle()), (('deferred', 0, 'interrupted'), 'training'))
        self.attempt()
        self.assertEqual((self.job()[1], self.cycle(), self.trained), ('staged', 'staged', [100]))
