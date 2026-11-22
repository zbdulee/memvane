"""M10 (selector maintenance request): every file shape under every event.

Real writers: ``_request_owned`` (the request and its spawn) and
``_record_request_result`` (the child's result). The clock, the spawn and the
route proof are stand-ins of ``test_selector_retry``'s fixture.
"""
from __future__ import annotations

import os
from unittest import mock

from memvane.journal.api import ledger_read_json
from memvane.learning import selector_maintenance as maintenance
from memvane.learning.context_learning.state_model import ABSENT, M10
from memvane.learning.tests.integration import test_selector_retry as retry_tests

SECOND = retry_tests.SECOND
LATER = 10 ** 6  # Past every floor and backoff of a row written around second 1000.


class SelectorRequestTable(retry_tests.MaintenanceRetry):
    # The fixture only: its own tests run in their module.
    locals().update({name: None for name in dir(retry_tests.MaintenanceRetry) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        self.stack.enter_context(mock.patch('memvane.runtime.api.request', return_value={'status': 'queued'}))

    def fresh(self):
        if self.path.exists():
            self.path.unlink()

    def row(self):
        return ledger_read_json(self.path) if self.path.exists() else None

    def observed(self):
        row = self.row()
        if row is None:
            return ABSENT
        if row['retryAfterNs'] == 0:
            return 'requested'
        return 'busy_retry' if 0 < row.get('busyRetries', 0) < maintenance.BUSY_RETRY_LIMIT else 'backoff'

    def build(self, state):
        self.fresh()
        if state == ABSENT:
            return
        self.assertEqual(self.request(1000), 1)
        if state == 'backoff':
            self.settle(1001)
        elif state == 'busy_retry':
            self.settle(1001, busy=True)

    def result(self, seconds, **kind):
        """The child's result for the request on file (none on file: its own stale stamp)."""
        if self.row() is None:
            with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': '1'}):
                return maintenance._record_request_result(self.decision, **kind)
        return self.settle(seconds, **kind)

    def drive(self, event):
        if event == 'request_due':
            self.assertEqual(self.request(LATER), 1)
        elif event == 'spawn_failed':
            with mock.patch.object(maintenance.time, 'time_ns', return_value=LATER * SECOND), \
                    mock.patch.object(maintenance.subprocess, 'Popen', side_effect=OSError('private')), \
                    self.assertRaises(OSError):
                maintenance._request_owned(self.decision, object(), force=False)
        elif event == 'failed':
            self.result(1002)
        elif event == 'busy':
            self.result(1002, busy=True)

    def test_every_request_shape_under_every_event_ends_where_the_model_says(self):
        cells, driven = 0, set()
        for state in M10.states:
            for event in M10.events:
                with self.subTest(state=state, event=event):
                    self.build(state)
                    self.assertEqual(self.observed(), state)
                    self.drive(event)
                    self.assertEqual(self.observed(), M10.next(state, event))
                cells += 1
                if (state, event) in M10.transitions:
                    driven.add((state, event))
        self.assertEqual((len(M10.states), len(M10.events), cells), (4, 4, 16))
        self.assertEqual(driven, set(M10.transitions))

    def test_a_request_before_the_due_time_changes_nothing(self):
        for state, early in (('requested', 1000 + maintenance.REQUEST_SECONDS - 1),
                             ('backoff', 1000 + maintenance.REQUEST_SECONDS - 1),
                             ('busy_retry', 1001 + maintenance.BUSY_RETRY_SECONDS - 1)):
            with self.subTest(state=state):
                self.build(state)
                before = self.row()
                self.assertEqual(self.request(early), 0)
                self.assertEqual(self.row(), before)

    def test_a_completed_run_clears_the_counts_and_a_stale_result_writes_nothing(self):
        self.build('backoff')
        self.assertEqual(self.request(LATER), 1)
        self.assertEqual(self.row()['failures'], 1)
        with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(1000 * SECOND)}):
            self.assertIsNone(maintenance._record_request_result(self.decision, completed=True))
        self.assertEqual(self.row()['failures'], 1)
        self.settle(LATER + 1, completed=True)
        self.assertEqual(self.row(), {'requestedNs': LATER * SECOND, 'failures': 0, 'retryAfterNs': 0})
        self.assertEqual(self.observed(), 'requested')

    def test_the_backoff_doubles_to_an_hour_and_the_busy_run_ends_at_its_limit(self):
        self.build(ABSENT)
        clock, delays = 1000, []
        for _ in range(9):
            self.assertEqual(self.request(clock), 1)
            row = self.settle(clock + 1)
            delays.append((row['retryAfterNs'] - (clock + 1) * SECOND) // SECOND)
            clock = row['retryAfterNs'] // SECOND
        self.assertEqual(delays[-3:], [3600, 3600, 3600])
        self.assertEqual(delays, sorted(delays))
        self.build('requested')
        clock = 1000
        for count in range(1, maintenance.BUSY_RETRY_LIMIT + 1):
            self.assertEqual(self.settle(clock + 1, busy=True)['busyRetries'], count)
            clock += 1 + maintenance.BUSY_RETRY_SECONDS
            self.assertEqual(self.request(clock), 1)
        self.assertEqual(self.observed(), 'requested')
        self.settle(clock + 1, busy=True)
        self.assertEqual((self.observed(), self.row()['failures']), ('backoff', 1))
