"""Actual selector entry and UNIX inference socket, with no model loading."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import runpy
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

from tests.support.case import FixtureCase, wait_until
from memvane.learning import selector_maintenance as maintenance
from memvane.learning.context_learning import inference_service as service
from memvane.learning.context_learning import selector_heal as heal, resident
from memvane.runtime.types import ServiceFailure


class WarmLifecycle(FixtureCase):
    def setUp(self):
        super().setUp()
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.server = self.home / 'server'
        self.server.mkdir(mode=0o700)
        self.policy = {'schema': 'qmd-live-selector-v1', 'timeoutSeconds': 30,
                       'maxRssMib': 4096, 'trainerArgv': [sys.executable, '/synthetic/adapter', '/synthetic/model']}
        self.pointer = {'schema_version': 1, 'artifact_path': str(self.state / 'base.artifact'),
                        'artifact_sha256': 'a' * 64, 'cycle_id': 'base'}
        self.save('live-selector.json', self.policy)
        self.save('active-checkpoint.json', self.pointer)
        self.decision = {'projectRoot': str(self.home), 'journalPath': str(self.state / 'journal'),
                         'storePath': str(self.state / 'store'), 'installId': 'fixture'}

    def save(self, name, value):
        path = self.state / name
        path.write_text(json.dumps(value), encoding='utf8')
        path.chmod(0o600)

    def run_entry(self, *, startup=0, loading=0, load_error=False, startup_error=None, policy_error=None):
        stop, ready = threading.Event(), threading.Event()
        self.start_calls = 0
        threads, envelopes, events = [], [], []
        controller = SimpleNamespace(inference=lambda *a: nullcontext(),
                                     inference_compute=lambda *a: nullcontext())
        def load(_job):
            time.sleep(loading)
            if load_error:
                raise ValueError('private synthetic path must not be logged')
            return object()
        engine = service.GenerationEngine(load=load, forward=lambda *a, **k: {'synthetic': True},
                    admission=lambda *a, **k: ({}, {}), rss=lambda: 100)
        def validate(envelope):
            deadline, _ = resident._request_window(envelope, now_epoch=time.time(), now_monotonic=time.monotonic())
            if not any(envelope is previous for previous in envelopes):
                envelopes.append(envelope)
            return {'namespace': envelope['namespace'], 'generation': ('r', 'm', 'v', 'a'),
                    'family': ('r', 'm', 'v'), 'policy': self.policy, 'request': envelope['request'],
                    'deadline': deadline, 'identity_timing': {}}
        def launch(**_kwargs):
            self.start_calls += 1
            if startup_error is not None:
                raise startup_error
            def serve():
                time.sleep(startup)
                service._serve(self.server, stop=stop, engine=engine, validate=validate, ready=ready, hosted=True)
            thread = threading.Thread(target=serve, daemon=True)
            threads.append(thread)
            thread.start()
            return 'starting'
        def actual_warm(*_args, **_kwargs):
            result = heal.warm(self.home, state=self.state)
            self.last_warm_result = result
            return result['status'] == 'selector_warm'
        entry = Path(__file__).resolve().parents[4] / 'entry/run.py'
        from memvane.runtime import api as runtime
        with mock.patch.object(runtime, 'coordinator', return_value=controller), \
                mock.patch.object(service, 'service_root', return_value=self.server), \
                mock.patch.object(service, '_signature', return_value='synthetic-signature'), \
                mock.patch.object(service, '_policy', return_value=(self.policy, self.policy), side_effect=policy_error), \
                mock.patch.object(heal, '_warm_key', return_value='synthetic-generation'), \
                mock.patch.object(heal, 'STARTUP_SECONDS', 3), \
                mock.patch.object(heal, 'WARM_SECONDS', 1), \
                mock.patch.object(heal, 'WARM_DRAIN_SECONDS', 3), \
                mock.patch('memvane.resident.api.ensure_service', side_effect=launch), \
                mock.patch.object(maintenance, '_run', side_effect=actual_warm), \
                mock.patch('memvane.journal.api.select_route', return_value=self.decision), \
                mock.patch('memvane.journal.api.verify_selected_route_for_write'), \
                mock.patch('memvane.diagnostics.api.emit', side_effect=lambda event, **fields: events.append((event, fields))), \
                mock.patch.object(sys, 'argv', [str(entry), 'selector-maintenance', str(self.home), 'force']):
            try:
                with self.assertRaises(SystemExit) as ended:
                    runpy.run_path(str(entry), run_name='__main__')
                self.assertEqual(ended.exception.code, 0)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(8)
                    self.assertFalse(thread.is_alive(), 'synthetic server must be reaped')
        return envelopes, events

    def test_slow_ready_does_not_consume_fresh_warm_window_through_entry(self):
        envelopes, events = self.run_entry(startup=1.2)
        self.assertTrue((self.state / heal.WARM_MARKER).is_file())
        self.assertEqual(len(envelopes), 1)
        self.assertGreater(envelopes[0]['deadline_epoch'] - envelopes[0]['queued_at_epoch'], .8)
        self.assertTrue(any(e == 'learning.selector.maintenance' and f.get('outcome') == 'completed'
                            for e, f in events))

    def test_cold_load_over_attempt_budget_finishes_on_one_bounded_followup(self):
        envelopes, _events = self.run_entry(loading=1.2)
        self.assertTrue((self.state / heal.WARM_MARKER).is_file())
        self.assertEqual(len(envelopes), 2)
        self.assertTrue(all(0 < row['deadline_epoch'] - row['queued_at_epoch'] <= 30 for row in envelopes))
        self.assertGreater(self.last_warm_result['timing']['cached_model_load_seconds'], 0)

    def test_load_failure_has_numeric_phase_timing_without_private_text(self):
        _envelopes, events = self.run_entry(loading=.02, load_error=True)
        failure = next(f for e, f in reversed(events) if e == 'learning.selector.maintenance'
                       and f.get('reason') == 'load_failed')
        self.assertGreater(failure['model_load_seconds'], 0)
        self.assertGreaterEqual(failure['startup_wait_seconds'], 0)
        self.assertNotIn('private synthetic path', json.dumps(events))

    def test_startup_deferral_retains_its_measured_wait_and_does_not_write_marker(self):
        from memvane.kernel.resource_deferred import ResourceDeferred
        _envelopes, events = self.run_entry(startup_error=ResourceDeferred('cpu_budget_busy'))
        failure = next(f for e, f in reversed(events) if e == 'learning.selector.maintenance'
                       and f.get('reason') == 'resource_deferred')
        self.assertGreaterEqual(failure['startup_wait_seconds'], 0)
        self.assertFalse((self.state / heal.WARM_MARKER).exists())

    def test_client_validation_failure_retains_timing_at_maintenance_boundary(self):
        _envelopes, events = self.run_entry(policy_error=ValueError('selector_policy_invalid'))
        failure = next(f for e, f in reversed(events) if e == 'learning.selector.maintenance'
                       and f.get('reason') == 'selector_policy_invalid')
        self.assertEqual(failure['stage'], 'validation')
        self.assertGreaterEqual(failure['client_validation_seconds'], 0)
        self.assertEqual(self.start_calls, 0)

    def test_startup_deadline_does_not_trigger_a_second_startup_as_warm_retry(self):
        envelopes, _events = self.run_entry(startup=3.2)
        self.assertEqual(self.start_calls, 1)
        self.assertEqual(envelopes, [])
        self.assertFalse((self.state / heal.WARM_MARKER).exists())

    def test_timing_fields_are_closed_finite_numeric_metadata(self):
        from memvane.learning.context_learning.inference_timing import clean
        result = clean({'startup_wait_seconds': 1, 'model_load_seconds': float('nan'),
                        'forward_seconds': True, 'identity_seconds': float('inf'),
                        'revalidation_seconds': -1, 'private': '/synthetic/secret'})
        self.assertEqual(result, {'startup_wait_seconds': 1})

    def test_warm_wait_metric_excludes_the_separate_startup_wait(self):
        from memvane.learning.context_learning import warm_lifecycle
        now = [0.]
        def predict(*_args, **_kwargs):
            now[0] += 7
            return {}, {'startup_wait_seconds': 5}
        with mock.patch.object(warm_lifecycle.time, 'monotonic', side_effect=lambda: now[0]):
            timing = warm_lifecycle.attempt(predict, self.state, self.policy, {}, 'a' * 64,
                                            startup_seconds=60, warm_seconds=30, drain_seconds=60)
        self.assertEqual(timing['warm_wait_seconds'], 2)

    def test_startup_deadline_failure_is_not_a_cold_load_followup(self):
        from memvane.learning.context_learning import warm_lifecycle
        error = ServiceFailure('deadline_expired', timing={'startup_wait_seconds': 60})
        error.phase = 'startup'
        with mock.patch.object(warm_lifecycle, '_drained') as drained:
            with self.assertRaises(ServiceFailure) as caught:
                warm_lifecycle.attempt(mock.Mock(side_effect=error), self.state, self.policy, {}, 'a' * 64,
                                       startup_seconds=60, warm_seconds=30, drain_seconds=60)
        drained.assert_not_called()
        self.assertIs(caught.exception, error)

    def test_wire_rejects_more_than_thirty_seconds(self):
        now = time.time()
        with self.assertRaisesRegex(ValueError, 'resident_wire_invalid'):
            resident._request_window({'queued_at_epoch': now, 'deadline_epoch': now + 31},
                                     now_epoch=now, now_monotonic=time.monotonic())
