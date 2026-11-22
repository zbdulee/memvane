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
from memvane.learning.context_learning.state_model import ABSENT, M1T, M4
from memvane.learning.tests.support import invariants
from memvane.learning.tests.integration.test_carry_122 import CarryFixture, FakeTrainer

TRAIN_EVENTS = ('train_started', 'trained', 'train_failed', 'train_deferred')
# Driven by ``EvaluationAndApplication`` below (``carried`` by the real renewal of
# ``memvane/learning/tests/integration/test_carry_122.py``).
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
        if step == 'intent_changed':
            raise ValueError('learning_train_intent_changed')
        if step == 'defer':
            raise automatic_staging.ResourceDeferred('trainer_failed:learning_yield', {'reason': 'learning_yield'})
        if step == 'artifact_then_fail':
            super().call(mode, request, result_path, cwd=cwd)
            raise ValueError('trainer_failed')
        if step == 'end_process':
            raise ProcessEnded()
        if step == 'artifact_then_end':
            super().call(mode, request, result_path, cwd=cwd)
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

    def marks(self):
        """How many gold cases carry each training mark (M1T)."""
        with shared._db(self.root) as db:
            return dict(db.execute("SELECT CASE WHEN trained_cycle IS NULL THEN 'unmarked' ELSE 'marked' END,"
                                   "COUNT(*) FROM cases WHERE status='gold' GROUP BY 1"))

    def violations(self):
        """Violations of the job and cycle rules. The fixture's gold cases are inserted
        as rows, without the teacher call a ledger row records, so I-3 is not asked."""
        return [line for line in invariants.check(self.root) if not line.startswith('I-3 ')]

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
                    self.assertEqual(self.violations(), [])
                    # M1T: the members are marked in the transaction that stages the cycle.
                    marked = M1T.next('unmarked', 'cycle_staged') if expected == 'staged' else 'unmarked'
                    self.assertEqual(self.marks(), {marked: 100})
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


class EvaluationAndApplication(CycleFixture):
    """The evaluation and application events of M4, through the worker command."""

    def pointer(self):
        return json.loads((self.root / 'active-checkpoint.json').read_text())

    def evaluation(self):
        return next(iter(self.evaluations().values()), (None, None))[0]

    def test_a_staged_cycle_is_evaluated_and_an_improved_candidate_is_applied(self):
        self.attempt()
        self.assertEqual((self.cycle(), self.evaluation()), ('staged', 'waiting_quality'))
        before = self.pointer()
        self.golds('v', 50, empty=10)
        result = self.attempt()
        self.assertEqual(result['status'], 'applied', result)
        # staged -> staged_evaluated -> applied in one pass (M4 ``evaluated``, ``applied``).
        self.assertEqual((self.cycle(), self.evaluation()), (M4.next(M4.next('staged', 'evaluated'), 'applied'),
                                                             'completed'))
        after = self.pointer()
        self.assertNotEqual(after, before)
        first = self.job()[0]
        self.assertEqual(after['cycle_id'], first)
        self.assertEqual(self.violations(), [])
        # A later pass plans the next job; the applied cycle and the pointer stay.
        self.due_now()
        self.worker()
        self.assertEqual((self.pointer(), self.cycle_rows()[first]), (after, 'applied'))
        self.assertEqual(self.violations(), [])

    def test_an_evaluated_candidate_that_cannot_be_applied_yet_is_evaluated_again_when_stale(self):
        from memvane.learning.context_learning.local_cycle import ResourceDeferred

        def busy(command, record):
            raise ResourceDeferred('apply_busy')
        self.attempt()
        self.golds('v', 50, empty=10)
        self.transport = busy
        self.assertEqual(self.attempt()['status'], 'apply_deferred')
        self.assertEqual((self.cycle(), self.evaluation()), ('staged_evaluated', 'completed'))
        scored = len(self.predicted)
        # The evaluation grows old while the application waits (M4 ``reevaluation_needed``).
        with shared._db(self.root) as db:
            evidence = json.loads(db.execute('SELECT evidence FROM cycles').fetchone()[0])
            evidence['metrics']['evaluatedAt'] = 1
            db.execute('UPDATE cycles SET evidence=?', (json.dumps(evidence, sort_keys=True, separators=(',', ':')),))
        del self.transport
        with shared._db(self.root) as db:
            db.execute("UPDATE learning_evaluations SET retry_at=0")
        journal = self.root / 'candidate-apply.json'
        waiting = json.loads(journal.read_text())
        waiting['retry_at'] = 0
        journal.write_text(json.dumps(waiting))
        journal.chmod(0o600)
        result = self.attempt()
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(len(self.predicted), scored + 2)
        self.assertEqual((self.cycle(), self.violations()), ('applied', []))


class StoppedApplications(CycleFixture):
    """An application stopped part way, then new gold, through the worker command."""

    def setUp(self):
        super().setUp()
        self.attempt()
        self.golds('v', 50, empty=10)

    def pointer(self):
        return json.loads((self.root / 'active-checkpoint.json').read_text())

    def journal(self):
        return json.loads((self.root / 'candidate-apply.json').read_text())

    def retry_now(self):
        path = self.root / 'candidate-apply.json'
        row = json.loads(path.read_text())
        row['retry_at'] = 0
        path.write_text(json.dumps(row))
        path.chmod(0o600)
        with shared._db(self.root) as db:
            db.execute('UPDATE learning_evaluations SET retry_at=0')

    def test_a_process_gone_after_the_resident_prepared_then_new_gold_ends_applied(self):
        from memvane.learning.context_learning import candidate_apply
        real = candidate_apply.apply_candidate

        def stop(at):
            if at == 'prepared':
                raise ProcessEnded()
        with mock.patch.object(candidate_apply, 'apply_candidate',
                               side_effect=lambda root, policy, cycle, **k: real(root, policy, cycle, fault=stop, **k)):
            self.assertEqual(self.attempt()['status'], 'process_ended')
        base = self.pointer()
        self.assertEqual((self.journal()['phase'], base['cycle_id']), ('prepared', 'base'))
        self.golds('w', 3)
        for _ in range(3):
            result = self.attempt()
            if result['status'] == 'applied':
                break
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual((self.journal()['phase'], self.pointer()['cycle_id']), ('activated', self.job()[0]))
        self.assertEqual(self.violations(), [])

    def test_a_resident_busy_after_the_pointer_then_one_gold_ends_applied(self):
        from memvane.learning.context_learning.local_cycle import ResourceDeferred
        calls = []

        def busy_once(command, record):
            calls.append(command)
            if len(calls) == 2:
                raise ResourceDeferred('apply_busy')
            return {'status': 'prepared' if command == 'prepare' else 'activated', 'token': record['apply_id']}
        self.transport = busy_once
        self.assertEqual(self.attempt()['status'], 'apply_deferred')
        self.assertEqual((self.journal()['phase'], self.pointer()['cycle_id']), ('pointer_committed', self.job()[0]))
        self.gold('one-more')
        self.retry_now()
        result = self.attempt()
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual((self.journal()['phase'], self.cycle_rows()[self.job()[0]]), ('activated', 'applied'))
        self.assertEqual(self.violations(), [])

    def test_a_candidate_that_can_never_form_its_intent_is_closed_with_the_reason_at_the_limit(self):
        from memvane.learning.context_learning import candidate_apply, inference_identity
        binding = self.root / inference_identity.BINDING
        value = json.loads(binding.read_text())
        value['receipt_sha256'] = '0' * 64
        binding.write_text(json.dumps(value))
        binding.chmod(0o600)
        first = self.job()[0]
        for count in range(1, candidate_apply.FAILURE_LIMIT + 1):
            result = self.attempt()
            self.assertEqual((result['status'], result['reason']),
                             ('apply_blocked', 'apply_prepared_inference_required'), result)
            status, reason = self.evaluations()[first]
            closed = count >= candidate_apply.FAILURE_LIMIT
            self.assertEqual((status, reason), ('blocked', 'apply_prepared_inference_required') if closed
                             else ('completed', None))
        self.assertFalse((self.root / 'candidate-apply.json').exists())
        self.assertEqual(self.pointer()['cycle_id'], 'base')
        # The closed candidate no longer holds the next job back.
        self.golds('n', 5)
        trained = len(self.trained)
        self.due_now()
        self.worker()
        self.assertEqual(len(self.trained), trained + 1)


class UnrepeatableFailures(CycleFixture):
    def test_a_failure_the_next_attempt_would_repeat_closes_the_job_at_once(self):
        self.assertEqual(self.attempt('intent_changed')['status'], 'failed')
        job_id, status, failures, reason, _retry = self.job()
        self.assertEqual((status, failures, reason), ('blocked', 0, 'learning_train_intent_changed'))
        self.assertEqual(self.outcomes()[-1], 'failed')
        # Not rearmed however long it waits; new gold pins another job, which trains.
        with shared._db(self.root) as db:
            db.execute('UPDATE learning_jobs SET retry_at=0')
        self.due_now()
        self.worker()
        self.assertNotIn('rearmed', self.outcomes())
        self.assertEqual(self.trained, [])
        self.golds('n', 3)
        self.due_now()
        self.worker()
        with shared._db(self.root) as db:
            rows = dict(db.execute('SELECT job_id,status FROM learning_jobs'))
        self.assertEqual((len(rows), rows[job_id], len(self.trained)), (2, 'blocked', 1))


class RevokedMembers(CycleFixture):
    def test_an_open_job_whose_member_row_is_gone_is_closed_and_the_next_plan_trains(self):
        self.assertEqual(self.attempt('fail')['status'], 'failed')
        first = self.job()
        self.assertEqual(first[1], 'deferred')
        with shared._db(self.root) as db:
            db.execute("DELETE FROM cases WHERE request_id='t-000'")
        self.gold('replacement')
        result = self.attempt()
        self.assertEqual(result['status'], 'staged_source_revoked', result)
        with shared._db(self.root) as db:
            rows = db.execute('SELECT job_id,status,reason FROM learning_jobs').fetchall()
        self.assertEqual(rows, [(first[0], 'blocked', 'source_revoked')])
        self.assertEqual([(e['outcome'], e.get('reason')) for e in self.cycle_events][-1],
                         ('blocked', 'source_revoked'))
        self.assertEqual(self.trained, [])
        self.due_now()
        self.worker()
        with shared._db(self.root) as db:
            rows = dict(db.execute('SELECT job_id,status FROM learning_jobs'))
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[first[0]], self.trained), ('blocked', [100]))
        self.assertEqual({status for job, status in rows.items() if job != first[0]}, {'staged'})

    def test_a_job_waiting_to_be_rearmed_whose_member_row_is_gone_is_closed_too(self):
        for _ in range(5):
            self.attempt('fail')
            if self.job()[1] == 'blocked':
                break
        first = self.job()
        self.assertEqual((first[1], first[3] in learning_queue.FINAL_BLOCKS), ('blocked', False))
        with shared._db(self.root) as db:
            db.execute("DELETE FROM cases WHERE request_id='t-000'")
        self.gold('replacement')
        self.assertEqual(self.attempt()['status'], 'staged_source_revoked')
        self.assertEqual(self.job()[:4], (first[0], 'blocked', first[2], 'source_revoked'))
        self.assertEqual([(e['outcome'], e.get('reason')) for e in self.cycle_events][-1],
                         ('blocked', 'source_revoked'))
        self.worker()
        with shared._db(self.root) as db:
            rows = dict(db.execute('SELECT job_id,status FROM learning_jobs'))
        self.assertEqual((len(rows), rows[first[0]], self.trained), (2, 'blocked', [100]))
        # A job closed for good keeps its reason: no second row, no second event.
        events = len(self.cycle_events)
        queue = learning_queue.LearningQueue(self.root)
        self.assertEqual(queue.block(first[0], 'already_consumed')['reason'], 'source_revoked')
        self.assertEqual(len(self.cycle_events), events)


class TrainingAttempts(CycleFixture):
    def test_each_job_and_cycle_invariant_names_the_state_that_breaks_it(self):
        self.attempt()
        self.assertEqual(self.violations(), [])
        job_id = self.job()[0]
        artifact = self.root / 'cycles' / job_id / 'candidate.artifact'
        kept = artifact.read_bytes()
        artifact.write_bytes(b'other bytes')
        self.assertEqual(self.violations(), ['I-4 cycle %s artifact differs from its evidence' % job_id])
        artifact.write_bytes(kept)
        with shared._db(self.root) as db:
            db.execute("UPDATE learning_jobs SET status='blocked',reason=NULL")
        found = self.violations()
        self.assertEqual(found[0], 'I-8 job %s blocked without a reason' % job_id)
        self.assertEqual(found[-1], 'I-4 cycle %s staged with job blocked' % job_id)
        self.assertEqual({line.split()[0] for line in found}, {'I-8', 'I-5', 'I-4'})
        with shared._db(self.root) as db:
            db.execute("UPDATE learning_jobs SET status='staged'")
            row = db.execute('SELECT namespace,approval,cohort,family,manifest FROM learning_jobs').fetchone()
            for name in ('twin-a', 'twin-b'):
                db.execute("INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,'queued',NULL,0,0,0,NULL)",
                           (name, *row))
        self.assertEqual(self.violations(), ['I-9 2 open jobs of one approval, cohort and family'])


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
        self.assertEqual([(e.get('cycle'), e.get('attempts')) for e in self.cycle_events
                          if e['outcome'] == 'rearmed'], [(job_id, 3)])
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

    def test_only_the_newest_set_aside_artifact_is_kept(self):
        from memvane.learning.context_learning import automatic_staging
        folder = self.tmp / 'cycle'
        folder.mkdir(mode=0o700)
        artifact = folder / 'candidate.artifact'
        kept = None
        for content in (b'first', b'second', b'third'):
            artifact.write_bytes(content)
            artifact.chmod(0o600)
            kept = automatic_staging._set_aside(artifact)
            self.assertFalse(artifact.exists())
        self.assertEqual([path.name for path in folder.iterdir()], [kept.name])
        self.assertEqual(kept.read_bytes(), b'third')

    def test_a_process_that_ends_while_training_leaves_a_job_the_next_pass_trains(self):
        self.assertEqual(self.attempt('end_process')['status'], 'process_ended')
        self.assertEqual((self.job()[1:4], self.cycle()), (('deferred', 0, 'interrupted'), 'training'))
        self.attempt()
        self.assertEqual((self.job()[1], self.cycle(), self.trained), ('staged', 'staged', [100]))
