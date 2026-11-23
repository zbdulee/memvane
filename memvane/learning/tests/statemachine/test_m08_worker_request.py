"""M8 (worker request): every state of a full-worker request under every event.

Real writers: the hook's ``launch``, the dispatcher's ``learning_worker_step``
and the owned child's ``_settle_owned``. The owned child is a tiny process
that only holds the inherited lease; no teacher, trainer or wake queue runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memvane.learning.context_learning import shared
from memvane.learning.context_learning.state_model import ABSENT, M8

REAL_POPEN = subprocess.Popen


class WorkerRequestTable(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '', 'QMD_SANDBOX': '', 'QMD_SHARED_GOLD_ONLY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.children = []
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.communicate(b'')
        self.children.clear()

    def fresh(self):
        self.reap()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        base.chmod(0o700)
        self.root, self.project = base / 'shared', base / 'project'
        self.root.mkdir(mode=0o700)
        self.project.mkdir()

    def spawn(self, argv, **kw):
        child = REAL_POPEN([sys.executable, '-B', '-c', 'import sys;sys.stdin.read()'], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                           pass_fds=kw['pass_fds'])
        self.children.append(child)
        return child

    # -- the real writers ----------------------------------------------------------
    def launch(self):
        with patch.object(shared, '_policy', return_value={'checked': True}), \
                patch.object(shared, '_safe_root', return_value=self.root), \
                patch.object(shared, 'telemetry_emit'), \
                patch('memvane.runtime.api.request', return_value={'status': 'queued'}):
            self.assertEqual(shared.launch({}, self.project), {'status': 'queued'})

    def step(self):
        answer = shared.learning_worker_step(self.root, self.project, 7, spawn=self.spawn)
        self.reap()  # The owned child ends: its lease is free for the next step.
        return answer

    def child_ends(self, status):
        request = shared._full_worker_request(self.root)
        state = shared._full_worker_state(self.root, request['nonce'])
        with patch('memvane.runtime.api.request', return_value={'status': 'queued'}):
            return shared._settle_owned(self.root, shared.FULL_WORKER_STATE, state, {'status': status}, self.project)

    def observed(self):
        request = shared._full_worker_request(self.root)
        if request is None:
            return ABSENT
        state = shared._full_worker_state(self.root, request['nonce'])
        if state['outcome'] is not None:
            return 'acknowledged' if state['acked'] else 'settled'
        return 'last_start' if state['starts'] >= shared.MAX_FULL_WORKER_STARTS else 'open'

    def build(self, state):
        self.fresh()
        if state == ABSENT:
            return
        self.launch()
        if state in ('last_start',):
            for _ in range(shared.MAX_FULL_WORKER_STARTS):
                self.assertEqual(self.step()['status'], 'started')
        elif state in ('settled', 'acknowledged'):
            self.assertEqual(self.step()['status'], 'started')
            self.child_ends('drained')
            if state == 'acknowledged':
                self.assertEqual(self.step()['status'], 'settled')

    def drive(self, state, event):
        if event == 'launch':
            self.launch()
        elif state == ABSENT:
            self.assertEqual(shared._full_step(self.root, self.project, -1, self.spawn), {'status': 'none'})
        elif event == 'third_start':
            if state == 'open':
                for _ in range(shared.MAX_FULL_WORKER_STARTS):
                    self.step()
        elif event == 'step':
            if state != 'open':
                self.step()
        elif event in ('child_result', 'child_retry_result'):
            if state in ('open', 'last_start'):
                self.child_ends('drained' if event == 'child_result' else 'deferred_resource')

    def test_every_request_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M8.states:
            for event in M8.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(state, event)
                    self.assertEqual(self.observed(), M8.next(state, event))
                cells += 1
                if (state, event) in M8.transitions:
                    driven.add((state, event))
        self.assertEqual(cells, len(M8.states) * len(M8.events))
        self.assertEqual((len(M8.states), len(M8.events)), (5, 5))
        self.assertEqual(driven, set(M8.transitions))

    def test_a_start_below_the_limit_keeps_the_request_open_and_counts(self):
        self.build('open')
        for starts in (1, 2):
            self.assertEqual(self.step(), {'status': 'started', 'mode': 'full', 'starts': starts})
            self.assertEqual(self.observed(), 'open')
        state = json.loads((self.root / shared.FULL_WORKER_STATE).read_text())
        self.assertEqual((state['starts'], state['outcome'], state['acked']), (2, None, False))

    def test_an_abandoned_request_says_so_and_a_retry_result_starts_the_count_again(self):
        self.build('last_start')
        self.assertEqual(self.step()['status'], 'abandoned_after_restarts')
        state = json.loads((self.root / shared.FULL_WORKER_STATE).read_text())
        self.assertEqual((state['outcome'], state['acked']), ('abandoned_after_restarts', True))
        self.build('last_start')
        answer = self.child_ends('deferred_resource')
        self.assertEqual(answer['learning_retry'], {'status': 'durable_wake_pending'})
        state = json.loads((self.root / shared.FULL_WORKER_STATE).read_text())
        self.assertEqual((state['starts'], state['outcome']), (0, None))


    def test_a_child_that_keeps_ending_with_retry_results_settles_the_request_at_the_limit(self):
        from memvane.learning.context_learning import worker_retry
        limit = worker_retry.RETRY_RESULT_LIMIT
        self.assertEqual(limit, 288)
        self.build('open')
        emitted, asked = [], []
        def ends(status):
            request = shared._full_worker_request(self.root)
            state = shared._full_worker_state(self.root, request['nonce'])
            with patch('memvane.runtime.api.request', side_effect=lambda *a, **k: asked.append(a) or {'status': 'queued'}), \
                    patch('memvane.diagnostics.api.emit', side_effect=lambda event, **f: emitted.append((event, f))), \
                    patch('memvane.journal.api.select_route', return_value={}):
                return shared._settle_owned(self.root, shared.FULL_WORKER_STATE, state, {'status': status}, self.project)
        # W15 r2 review F-9: retry results of different kinds in a row count together, so a
        # resource deferral or a busy lock in between never starts the count again.
        kinds = ('apply_deferred', 'deferred_resource', 'busy', 'staged_evaluation_deferred')
        for index in range(limit - 1):
            self.assertEqual(ends(kinds[index % len(kinds)])['learning_retry'], {'status': 'durable_wake_pending'})
        self.assertEqual((len(asked), self.observed(), emitted), (limit - 1, 'open', []))
        # The limit: no wake is asked for, the request is settled with the last result, one row says so.
        last = ends('apply_deferred')
        self.assertNotIn('learning_retry', last)
        self.assertEqual((len(asked), self.observed()), (limit - 1, 'settled'))
        self.assertEqual([(event, row['outcome'], row['reason'], row['retries']) for event, row in emitted],
                         [('learning.cycle', 'retry_cap', 'apply_deferred', limit)])
        self.assertEqual(self.step(), {'status': 'settled', 'outcome': 'apply_deferred'})
        self.assertFalse((self.root / worker_retry.NAME).exists())
        # The next hook launch opens a new request, and the count starts over.
        self.launch()
        self.assertEqual(self.observed(), 'open')
        self.assertEqual(ends('deferred_resource')['learning_retry'], {'status': 'durable_wake_pending'})

    def test_a_slice_that_advanced_clears_the_count_and_keeps_the_request(self):
        # Third-party review T-3: a job that keeps moving is never settled by the retry limit.
        from memvane.learning.context_learning import worker_retry
        self.fresh()
        limit = worker_retry.RETRY_RESULT_LIMIT
        with patch('memvane.diagnostics.api.emit', side_effect=AssertionError('settled')), \
                patch('memvane.journal.api.select_route', return_value={}):
            for index in range(2 * limit):
                result = {'status': 'deferred_resource', 'cycle': {'status': 'deferred_resource', 'progress': True}}
                self.assertTrue(worker_retry.again(self.root, self.project, 'deferred_resource', True, result))
                self.assertFalse((self.root / worker_retry.NAME).exists())
            # Without progress the same status counts from one.
            self.assertTrue(worker_retry.again(self.root, self.project, 'deferred_resource', True,
                                               {'status': 'deferred_resource', 'cycle': {'status': 'deferred_resource'}}))
        self.assertEqual(json.loads((self.root / worker_retry.NAME).read_text())['count'], 1)

    def test_deferrals_before_any_claim_reach_the_limit(self):
        # Supervisor's condition on T-3: the short wait before a claim is bounded by this count.
        from memvane.learning.context_learning import worker_retry
        limit = worker_retry.RETRY_RESULT_LIMIT
        self.fresh()
        emitted = []
        # What a pass answers when the resources were not ready before it claimed the job.
        cycle = {'status': 'deferred_resource', 'resources': {'ready': False}, 'cycle_id': 'c', 'next_retry': 30}
        result = {'status': 'deferred_resource', 'cycle': cycle}
        with patch('memvane.diagnostics.api.emit', side_effect=lambda event, **f: emitted.append((event, f))), \
                patch('memvane.journal.api.select_route', return_value={}):
            kept = [worker_retry.again(self.root, self.project, 'deferred_resource', True, result) for _ in range(limit)]
        self.assertEqual((kept.count(True), kept[-1]), (limit - 1, False))
        self.assertEqual([(event, row['outcome'], row['reason'], row['retries']) for event, row in emitted],
                         [('learning.cycle', 'retry_cap', 'deferred_resource', limit)])

    def test_a_result_that_is_not_a_retry_clears_the_count(self):
        from memvane.learning.context_learning import worker_retry
        self.build('open')
        for status, kept in (('deferred_resource', True), ('deferred_resource', True), ('waiting_fresh_train', False)):
            request = shared._full_worker_request(self.root)
            state = shared._full_worker_state(self.root, request['nonce'])
            if state['outcome'] is not None:
                break
            with patch('memvane.runtime.api.request', return_value={'status': 'queued'}):
                shared._settle_owned(self.root, shared.FULL_WORKER_STATE, state, {'status': status}, self.project)
            self.assertEqual((self.root / worker_retry.NAME).exists(), kept)

    def test_progress_after_a_long_run_without_any_keeps_the_request(self):
        # Review 10 (W15 F-11): progress is judged before the limit, also on the pass that would reach it.
        from memvane.learning.context_learning import worker_retry
        limit = worker_retry.RETRY_RESULT_LIMIT
        self.fresh()
        with patch('memvane.diagnostics.api.emit', side_effect=AssertionError('settled')), \
                patch('memvane.journal.api.select_route', return_value={}):
            for _ in range(limit - 1):
                self.assertTrue(worker_retry.again(self.root, self.project, 'deferred_resource', True,
                                                   {'status': 'deferred_resource'}))
            for status in ('deferred_resource', 'staged_evaluation_deferred'):
                moved = {'status': status, 'cycle': {'status': status, 'progress': True}}
                self.assertTrue(worker_retry.again(self.root, self.project, status, True, moved))
                self.assertFalse((self.root / worker_retry.NAME).exists())


if __name__ == '__main__':
    unittest.main()
