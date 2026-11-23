"""M11 (selector maintenance pass): every way a pass ends settles the request and says why.

The pass is ``_run`` (a stand-in here: its steps have their own tests); the
real recorder ``_run_recorded`` settles the request file and writes the
``learning.selector.maintenance`` row. The whole-run deadline is real.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import threading
from unittest import mock

from memvane.journal.api import ledger_read_json
from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.learning import selector_maintenance as maintenance
from memvane.learning.context_learning import selector_heal
from memvane.learning.context_learning.state_model import MAINTENANCE_RESULTS
from memvane.learning.tests.integration import test_selector_retry as retry_tests

SECOND = retry_tests.SECOND
# How long a stand-in step waits before it gives up: long against the patched deadline.
STUCK_SECONDS = 20
INCOMPLETE = [name for name, row in MAINTENANCE_RESULTS.items() if row[2] == 'maintenance_incomplete']


class MaintenanceResults(retry_tests.MaintenanceRetry):
    # The fixture only: its own tests run in their module.
    locals().update({name: None for name in dir(retry_tests.MaintenanceRetry) if name.startswith('test_')})

    def ended(self, work):
        """One recorded pass over a fresh request: (request row, the result row, offers, raised)."""
        if self.path.exists():
            self.path.unlink()
        self.assertEqual(self.request(1000), 1)
        from memvane.diagnostics import api as diagnostics
        diagnostics.emit.reset_mock()
        raised = None
        with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(1000 * SECOND)}), \
                mock.patch('memvane.journal.api.select_route', return_value=self.decision), \
                mock.patch('memvane.journal.api.verify_selected_route_for_write'), \
                mock.patch('memvane.runtime.api.request', return_value={'status': 'queued'}) as offer, \
                mock.patch.object(maintenance.time, 'time_ns', return_value=1001 * SECOND), \
                mock.patch.object(maintenance, '_run', side_effect=work):
            try:
                maintenance.run(self.home, force=True)
            except Exception as exc:
                raised = exc
        rows = [call.kwargs for call in diagnostics.emit.call_args_list
                if call.args[0] == 'learning.selector.maintenance']
        return ledger_read_json(self.path), rows[-1], offer.call_count, raised

    def settlement(self, row, offers):
        if row['retryAfterNs'] == 0:
            return 'completed'
        if row.get('busyRetries'):
            return 'busy'
        return 'kept' if not offers else 'backoff'

    def test_every_way_a_pass_ends_is_in_the_table(self):
        source = Path(maintenance.__file__).read_text(encoding='utf-8')
        self.assertEqual(sorted(set(re.findall(r"unsettled\('([a-z_]+)'\)", source))), sorted(INCOMPLETE))
        self.assertEqual({name: MAINTENANCE_RESULTS[name][3] for name in INCOMPLETE}, maintenance._INCOMPLETE)
        self.assertEqual(len(INCOMPLETE), 9)
        self.assertEqual(sorted(set(MAINTENANCE_RESULTS) - set(INCOMPLETE)),
                         ['completed', 'learning_prepare_busy', 'learning_runtime_pending',
                          'maintenance_failed', 'maintenance_timeout', 'resource_deferred'])

    def test_a_returned_result_settles_the_request_and_names_itself(self):
        def returning(value, reason=None):
            def work(project, *, force=False, incomplete=None):
                if reason is not None:
                    incomplete.append(reason)
                return value
            return work
        cases = [('completed', returning(True)), ('learning_runtime_pending', returning(None))]
        cases += [(name, returning(False, name)) for name in INCOMPLETE]
        for name, work in cases:
            with self.subTest(result=name):
                request, outcome, reason, stage = MAINTENANCE_RESULTS[name]
                row, event, offers, raised = self.ended(work)
                self.assertIsNone(raised)
                self.assertEqual(self.settlement(row, offers), {'deferred': 'backoff'}.get(request, request))
                self.assertEqual((event['outcome'], event['reason'], event['stage']), (outcome, reason, stage))
                self.assertEqual(event['incomplete'], name if name in INCOMPLETE else None)
                self.assertEqual(row['failures'], 0 if name == 'completed' else 1)

    def test_a_raised_end_settles_the_request_with_its_fixed_reason(self):
        def raising(error):
            def work(project, *, force=False, incomplete=None):
                raise error
            return work
        cases = [('learning_prepare_busy', selector_heal.HealDeferred(maintenance.PREPARE_BUSY), 'learning_prepare_busy', False),
                 ('resource_deferred', ResourceDeferred('private detail'), 'resource_deferred', True),
                 ('resource_deferred', selector_heal.HealDeferred('learning_prepare_memory_pressure'),
                  'learning_prepare_memory_pressure', True),
                 ('maintenance_failed', ValueError('private detail'), 'maintenance_failed', True),
                 ('maintenance_failed', selector_heal.HealRefused('learning_runtime_changed'), 'learning_runtime_changed', True),
                 ('maintenance_timeout', maintenance.MaintenanceTimeout(maintenance.MAINTENANCE_TIMEOUT),
                  'maintenance_timeout', False)]
        for name, error, reason, reraised in cases:
            with self.subTest(result=name, reason=reason):
                request, outcome, _reason, _stage = MAINTENANCE_RESULTS[name]
                row, event, offers, raised = self.ended(raising(error))
                self.assertEqual(raised is not None, reraised)
                self.assertEqual((event['outcome'], event['reason']), (outcome, reason))
                self.assertEqual(self.settlement(row, offers), {'deferred': 'backoff', 'failed': 'backoff'}.get(request, request))
                self.assertEqual(row['failures'], 0 if request == 'busy' else 1)
                self.assertEqual(offers, 1)

    def test_a_run_past_the_deadline_ends_with_the_timeout_and_restores_the_signal(self):
        release = threading.Event()
        self.addCleanup(release.set)
        swallowed, returned = [], []
        def stuck(project, *, force=False, incomplete=None):
            try:
                # A step that does not return in time. The wait is bounded so that a run
                # without a deadline comes back here and fails below instead of hanging.
                release.wait(STUCK_SECONDS)
                returned.append(True)
            except Exception:  # A step's own broad handler does not keep the run going.
                swallowed.append(True)
        before = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(maintenance, 'MAINTENANCE_SECONDS', 0.05):
            row, event, offers, raised = self.ended(stuck)
        self.assertEqual((returned, swallowed, raised), ([], [], None))
        self.assertEqual((event['outcome'], event['reason']), ('failed', 'maintenance_timeout'))
        self.assertEqual((row['failures'], row['retryAfterNs'] > 0, offers), (1, True, 1))
        self.assertIs(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_a_step_that_keeps_going_past_the_first_timeout_is_ended_by_the_next(self):
        release = threading.Event()
        self.addCleanup(release.set)
        caught, returned = [], []
        def stubborn(project, *, force=False, incomplete=None):
            try:
                release.wait(STUCK_SECONDS)
            except BaseException:  # Swallows the first timeout and goes on waiting.
                caught.append(True)
            release.wait(STUCK_SECONDS)
            returned.append(True)
        with mock.patch.object(maintenance, 'MAINTENANCE_SECONDS', 0.05), \
                mock.patch.object(maintenance, '_TIMEOUT_REPEAT_SECONDS', 0.05):
            row, event, _offers, raised = self.ended(stubborn)
        self.assertEqual((caught, returned, raised), ([True], [], None))
        self.assertEqual((event['outcome'], event['reason'], row['failures']), ('failed', 'maintenance_timeout', 1))
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_a_run_inside_the_deadline_leaves_no_timer(self):
        self.assertEqual(maintenance._bounded(lambda: 'done'), 'done')
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual((maintenance.MAINTENANCE_SECONDS, maintenance._TIMEOUT_REPEAT_SECONDS), (1800, 30))
