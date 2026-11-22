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


if __name__ == '__main__':
    unittest.main()
