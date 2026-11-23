"""M16 (selector warm-up): every marker state under every event.

Real writer: ``selector_heal.warm`` with its lock, key and marker. The
prediction and the identity of the running service are stand-ins.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning.context_learning import inference_service, selector_heal
from memvane.learning.context_learning.state_model import ABSENT, M16
from memvane.runtime.types import ServiceFailure

POLICY = {'trainerArgv': [sys.executable, 'adapter', 'model'], 'timeoutSeconds': 30, 'maxRssMib': 4096}


class WarmTable(unittest.TestCase):
    def fresh(self):
        temp = tempfile.TemporaryDirectory(prefix='mv-warm-')
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve()
        self.state.chmod(0o700)
        self.service = [11, 2, 3, 'sig']
        self.calls = 0
        self.checkpoint(b'head')

    def checkpoint(self, content):
        path = self.state / 'active-checkpoint.json'
        path.write_text(json.dumps({'schema_version': 1, 'artifact_path': str(self.state / 'head.artifact'),
                                    'artifact_sha256': hashlib.sha256(content).hexdigest()}))
        path.chmod(0o600)

    def warm(self, error=None):
        def predict(*_args, **_kwargs):
            self.calls += 1
            if error is not None:
                raise error
            return {}, {}
        with patch.object(inference_service, '_policy', return_value=(POLICY, POLICY)), \
                patch.object(inference_service, 'predict', side_effect=predict), \
                patch.object(selector_heal, '_service_identity', side_effect=lambda: self.service), \
                patch.object(selector_heal, 'STARTUP_SECONDS', 1), patch.object(selector_heal, 'WARM_SECONDS', 1), \
                patch.object(selector_heal, 'WARM_DRAIN_SECONDS', 1):
            return selector_heal.warm('/p', state=self.state)

    def marker(self):
        path = self.state / selector_heal.WARM_MARKER
        return path.read_bytes() if path.exists() else None

    def observed(self):
        if self.marker() is None:
            return ABSENT
        with patch.object(selector_heal, '_service_identity', side_effect=lambda: self.service):
            current = selector_heal._warm_key(self.state)
        return 'warm' if json.loads(self.marker())['key'] == current else 'stale'

    def build(self, state):
        self.fresh()
        if state != ABSENT:
            self.assertEqual(self.warm()['status'], 'selector_warm')
        if state == 'stale':
            self.service = [12, 2, 4, 'sig']

    def drive(self, event):
        if event == 'warm':
            self.warm()
        elif event == 'new_checkpoint':
            self.checkpoint(b'next')
        elif event == 'service_restart':
            self.service = [99, 2, 9, 'sig']

    def test_every_marker_state_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M16.states:
            for event in M16.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(event)
                    self.assertEqual(self.observed(), M16.next(state, event))
                cells += 1
                if (state, event) in M16.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M16.states), len(M16.events), cells), (3, 3, 9))
        self.assertEqual(driven, set(M16.transitions))

    def test_a_current_marker_sends_no_request_and_a_stale_one_sends_one(self):
        self.build('warm')
        self.assertEqual(self.warm(), {'status': 'selector_warm_current'})
        self.assertEqual(self.calls, 1)
        self.build('stale')
        self.assertEqual(self.warm()['status'], 'selector_warm')
        self.assertEqual(self.calls, 2)

    def test_a_deferred_a_failed_and_a_busy_warm_up_write_nothing(self):
        for state in (ABSENT, 'stale'):
            for error, raised in ((ServiceFailure('starting'), selector_heal.HealDeferred),
                                  (ResourceDeferred('resource_deferred'), selector_heal.HealDeferred),
                                  (ServiceFailure('load_failed'), ServiceFailure),
                                  (ValueError('private text'), ValueError)):
                with self.subTest(state=state, error=str(error)):
                    self.build(state)
                    before = self.marker()
                    with self.assertRaises(raised):
                        self.warm(error)
                    self.assertEqual((self.marker(), self.observed()), (before, state))
            with self.subTest(state=state, error='busy'):
                self.build(state)
                before, calls = self.marker(), self.calls
                held = os.open(self.state / selector_heal.WARM_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(held, fcntl.LOCK_EX)
                    self.assertEqual(self.warm(), {'status': 'selector_warm_busy'})
                finally:
                    os.close(held)
                self.assertEqual((self.marker(), self.calls), (before, calls))
