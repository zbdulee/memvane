"""Maintenance attempts retain bounded per-install retry state."""
from contextlib import ExitStack, nullcontext
import os
import sys
import time
from unittest import mock

from tests.support.case import FixtureCase
from memvane.learning import selector_maintenance as maintenance
from memvane.learning.context_learning import inference_service as service
from memvane.journal.api import ledger_read_json, ledger_write
from memvane.kernel.resource_deferred import ResourceDeferred

SECOND = 10 ** 9


class MaintenanceRetry(FixtureCase):
    def setUp(self):
        super().setUp()
        self.state = self.home / 'install'
        self.state.mkdir(mode=0o700)
        self.path = self.state / maintenance.REQUEST_FILE
        self.decision = {'projectRoot': str(self.home), 'journalPath': str(self.state / 'journal'),
                         'storePath': str(self.state / 'store'), 'installId': 'fixture'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        patches = [mock.patch('memvane.runtime.api.validate_sync_owner'),
                   mock.patch('memvane.runtime.api.sync_owner', side_effect=lambda *a, **k: nullcontext(object())),
                   mock.patch('memvane.journal.api.inspect_journal', return_value={
                       'state': 'selected', 'installId': 'fixture', 'maxPluginVersion': '1.0.9'}),
                   mock.patch('memvane.kernel.memvane_version.plugin_version', return_value='1.0.9'),
                   mock.patch('memvane.learning.default_enrollment.record', return_value={'status': 'pending_runtime'}),
                   mock.patch('memvane.diagnostics.api.emit')]
        for patch in patches:
            self.stack.enter_context(patch)

    def request(self, seconds):
        with mock.patch.object(maintenance.time, 'time_ns', return_value=seconds * SECOND), \
                mock.patch.object(maintenance.subprocess, 'Popen') as spawned:
            maintenance._request_owned(self.decision, object(), force=False)
        return spawned.call_count

    def settle(self, seconds, *, deferred=False, completed=False):
        request = ledger_read_json(self.path)['requestedNs']
        with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': str(request)}), \
                mock.patch.object(maintenance.time, 'time_ns', return_value=seconds * SECOND):
            maintenance._record_request_result(self.decision, deferred=deferred, completed=completed)

    def test_persistent_failures_back_off_and_same_request_is_counted_once(self):
        self.assertEqual(self.request(1000), 1)
        self.settle(1001)
        first = ledger_read_json(self.path)
        self.assertEqual(first['requestedNs'], 1000 * SECOND)
        self.assertEqual(first['failures'], 1)
        self.assertEqual(first['retryAfterNs'], 1601 * SECOND)
        self.settle(1002)
        self.assertEqual(ledger_read_json(self.path), first)
        self.assertEqual(self.request(1600), 0)
        self.assertEqual(self.request(1602), 1)
        self.settle(1603)
        self.assertEqual(ledger_read_json(self.path)['retryAfterNs'], 2803 * SECOND)
        self.assertEqual(ledger_read_json(self.path)['failures'], 2)

    def test_deferral_retains_floor_and_success_clears_failure_count(self):
        self.assertEqual(self.request(1000), 1)
        self.settle(1001, deferred=True)
        row = ledger_read_json(self.path)
        self.assertEqual(row['requestedNs'], 1000 * SECOND)
        self.assertGreaterEqual(row['retryAfterNs'], 1600 * SECOND)
        self.assertEqual(self.request(1002), 0)
        self.assertEqual(self.request(1602), 1)
        self.settle(1603, completed=True)
        row = ledger_read_json(self.path)
        self.assertEqual((row['failures'], row['retryAfterNs']), (0, 0))

    def test_old_request_shape_is_supported_and_retry_delay_is_capped(self):
        ledger_write(self.path, {'requestedNs': 1000 * SECOND}, replace=False)
        self.assertEqual(self.request(1001), 0)
        self.assertEqual(self.request(1601), 1)
        row = ledger_read_json(self.path)
        ledger_write(self.path, dict(row, failures=15), replace=True)
        self.settle(1602)
        self.assertEqual(ledger_read_json(self.path)['retryAfterNs'], 5202 * SECOND)

    def test_stale_worker_cannot_update_a_newer_request(self):
        self.assertEqual(self.request(1000), 1)
        before = ledger_read_json(self.path)
        with mock.patch.dict(os.environ, {'MEMVANE_SELECTOR_REQUEST_NS': '1'}):
            maintenance._record_request_result(self.decision)
        self.assertEqual(ledger_read_json(self.path), before)

    def test_spawn_failure_keeps_the_request_and_backoff(self):
        with mock.patch.object(maintenance.time, 'time_ns', return_value=1000 * SECOND), \
                mock.patch.object(maintenance.subprocess, 'Popen', side_effect=OSError('private')):
            with self.assertRaises(OSError):
                maintenance._request_owned(self.decision, object(), force=False)
        row = ledger_read_json(self.path)
        self.assertEqual((row['requestedNs'], row['failures'], row['retryAfterNs']),
                         (1000 * SECOND, 1, 1600 * SECOND))
        self.assertEqual(self.request(1001), 0)

    def test_same_version_pending_enrollment_still_launches_force_mode(self):
        with mock.patch.object(maintenance.time, 'time_ns', return_value=1000 * SECOND), \
                mock.patch.object(maintenance.subprocess, 'Popen') as spawned:
            maintenance._request_owned(self.decision, object(), force=False)
        self.assertEqual(spawned.call_args.args[0][-1], 'force')

    def test_permanent_start_failure_keeps_its_fixed_event_reason(self):
        from memvane.runtime.types import ServiceFailure
        with mock.patch.object(maintenance, '_run', side_effect=ServiceFailure('start_refused')), \
                mock.patch('memvane.journal.api.select_route', return_value=self.decision), \
                mock.patch('memvane.journal.api.verify_selected_route_for_write'), \
                mock.patch('memvane.diagnostics.api.emit') as emit:
            with self.assertRaises(ServiceFailure):
                maintenance.run(self.home, force=True)
        self.assertEqual(emit.call_args.kwargs['reason'], 'start_refused')
        self.assertEqual(emit.call_args.kwargs['outcome'], 'failed')


class StartupFailures(FixtureCase):
    def test_service_start_failures_have_specific_local_codes(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        failures = [(ResourceDeferred('laya_service_replacement_refused'), 'start_refused'),
                    (ResourceDeferred('live_write_requires_plugin_cache'), 'start_path_invalid'),
                    (ResourceDeferred('cpu_budget_busy'), 'resource_deferred'),
                    (PermissionError('private detail'), 'permission_denied'),
                    (OSError('private detail'), 'start_io_failed'),
                    (RuntimeError('private detail'), 'start_failed')]
        for error, expected in failures:
            with self.subTest(expected=expected), mock.patch.object(service, '_hosted_ready', return_value=False):
                with self.assertRaises(service.ServiceFailure) as found:
                    service._ensure(root, sys.executable, time.monotonic() + 1,
                                    ensure_service=mock.Mock(side_effect=error))
                self.assertEqual(found.exception.code, expected)
                self.assertNotIn('private detail', str(found.exception))
