#!/usr/bin/env python3
"""Actual hosted ensure and load/admission retry gates on synthetic evidence.

No sockets, service subprocess, model, monitor or operational queue starts.
Host hello/learning version/OS reports and scheduling are synthetic. Version
policy, Service._load/_load_attested and Coordinator.check/latch files are real.
"""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))
from context_learning import inference_service as learning
from context_learning import resource_coordination as resources
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as host
from laya_index_a import IndexContractError

spec = importlib.util.spec_from_file_location('resident_fixture_for_retry_gates',
                                             HERE / 'laya-resident-service-checks.py')
FIXTURE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FIXTURE)
REAL_ENSURE = host.ensure_service


def emit(row):
    print(json.dumps(row, sort_keys=True), flush=True)


class InlineThread:
    """Only deterministic scheduling; the real _load worker body still runs."""
    def __init__(self, target, args=(), **_kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


class RuntimeGateChecks(unittest.TestCase):
    def test_old_client_rejects_ready_hosted_learning_mismatch_without_polling(self):
        with tempfile.TemporaryDirectory(prefix='rt-version-', dir='/tmp') as temp:
            base = Path(temp).resolve()
            root, host_root = base / 'learning', base / 'host'
            root.mkdir(mode=0o700)
            host_root.mkdir(mode=0o700)
            clock = [100.0]
            sleeps, drains, spawns, states, versions, failures = [], [], [], [], [], []
            learning_answer = {'status': 'ready', 'protocol': learning.PROTOCOL,
                               'signature': 'synthetic-newer-signature', 'hosted': True,
                               'python': sys.executable, 'busy': False, 'queued': 0}
            host_answer = {'status': 'ready', 'protocols': [host.PROTOCOL],
                           'bundle': {'version': '2.0.0', 'digest': 'synthetic-new'},
                           'python': sys.executable}
            def version(_root, _deadline):
                versions.append(clock[0])
                return dict(learning_answer)
            def sleep(seconds):
                self.assertGreater(seconds, 0)
                sleeps.append(seconds)
                clock[0] += seconds
            def ensure(**kwargs):
                state = REAL_ENSURE(root=host_root, legacy_root=None, **kwargs)
                states.append(state)
                return state
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {'HOME': str(base),
                    'QMD_SETUP_GUARD_FIXTURE': '1', 'MEMVANE_TEST_RUNTIME_ROOT': str(base)}))
                stack.enter_context(mock.patch.object(learning, '_signature', return_value='synthetic-old-signature'))
                stack.enter_context(mock.patch.object(learning, '_version', version))
                stack.enter_context(mock.patch.object(learning.time, 'monotonic', lambda: clock[0]))
                stack.enter_context(mock.patch.object(learning.time, 'sleep', sleep))
                stack.enter_context(mock.patch.object(host, 'bundle_identity',
                    return_value={'version': '1.0.0', 'digest': 'synthetic-old'}))
                stack.enter_context(mock.patch.object(host, 'hello', return_value=host_answer))
                stack.enter_context(mock.patch.object(host, 'exchange',
                    side_effect=lambda *a, **k: drains.append(a) or {'status': 'draining'}))
                stack.enter_context(mock.patch.object(host, '_spawn',
                    side_effect=lambda *a, **k: spawns.append(a)))
                stack.enter_context(mock.patch.object(host, 'ensure_service', side_effect=ensure))
                started = clock[0]
                for _ in range(2):
                    try:
                        learning._ensure(root, sys.executable, clock[0] + 2)
                    except learning.ServiceFailure as exc:
                        failures.append(exc.code)
                    else:
                        failures.append('returned')
            emit({'case': 'ready-host-version-mismatch', 'failures': failures,
                  'hostStates': states, 'versionObservations': len(versions),
                  'pollSleeps': len(sleeps), 'elapsedSynthetic': clock[0] - started,
                  'drains': len(drains), 'spawns': len(spawns)})
            self.assertEqual(failures, ['version_mismatch', 'version_mismatch'])
            self.assertEqual(sleeps, [])
            self.assertEqual(drains, [])
            self.assertEqual(spawns, [])
            self.assertEqual(clock[0], started)

    def test_failed_start_then_pressure_recovers_through_real_admission_windows(self):
        for load, window in ((0.2, resources.RECOVERY_SECONDS),
                             (0.8, resources.RECOVERY_BUSY_SECONDS)):
            with self.subTest(load=load), ExitStack() as cleanup:
                case = FIXTURE.Fixture('runTest')
                case.setUp()
                cleanup.callback(case.doCleanups)
                cleanup.enter_context(mock.patch.dict(os.environ, {'HOME': str(case.base),
                    'QMD_SETUP_GUARD_FIXTURE': '1', 'MEMVANE_TEST_RUNTIME_ROOT': str(case.base)}))
                project, manifest = case.project('p')
                decision, proof, mode = case.prove(project)
                lane = case.base / 'resource-lane'
                lane.mkdir(mode=0o700)
                now = [100.0]
                phase = ['calm']
                controller = resources.Coordinator(lane, clock=lambda: now[0],
                                                    wall=lambda: 1000 + now[0])
                policy = {'maxLoadPerCpu': 1, 'minAvailableMib': 256,
                          'maxRssMib': 512, 'targetHeadroomFraction': 0.35}
                observations, starts, attests = [], [], []
                def report():
                    return {'ready': True, 'observed_at_mono': now[0],
                        'total_mib': 16384, 'available_mib': 8192,
                        'load_per_cpu': 2.0 if phase[0] == 'pressure' else load,
                        'swap_used_mib': 0, 'query_status': {'memory_pressure': 'ok'},
                        'memory_pressure': {'level': 'normal', 'system_free_percent': 80}}
                def admit():
                    result = controller.check(policy, report())
                    observations.append({'at': now[0], 'ready': result['ready'],
                                         'reason': result['reason']})
                    return result['ready']
                def start(_manifest, _admit):
                    starts.append(now[0])
                    if len(starts) == 1:
                        raise IndexContractError('synthetic_start_failure')
                    return SimpleNamespace(process=SimpleNamespace(poll=lambda: None), stop=lambda: None)
                service = host.Service(case.root, bundle={'version': '1.0.0', 'digest': 'synthetic'},
                    prove=case.prove, attest=lambda *a: attests.append(now[0]),
                    start_worker=start, admit=admit, retire=lambda *a: None)
                cleanup.callback(service.close)
                key = host._worker_key(manifest)
                cleanup.enter_context(mock.patch.object(host.time, 'monotonic', lambda: now[0]))
                cleanup.enter_context(mock.patch.object(owner.time, 'time', lambda: 1000 + now[0]))
                cleanup.enter_context(mock.patch.object(host.threading, 'Thread', InlineThread))
                def tick():
                    service._load(project, decision, proof, mode, manifest, key, 'synthetic-seal')
                tick()
                worker = service.workers[key]
                self.assertEqual((len(starts), worker.failures, worker.state), (1, 1, 'failed'),
                                 'setup must produce exactly one real start failure')
                self.assertEqual(worker.retry_at, 110)
                now[0], phase[0] = 110.0, 'pressure'
                tick()
                latch = json.loads((lane / 'resource-recovery.json').read_text())
                self.assertTrue(latch['paused'], 'actual pressure observation must latch paused')
                self.assertEqual((len(starts), worker.failures, worker.state),
                                 (1, 1, 'resource_deferred'))
                calm_since = 111
                phase[0] = 'calm'
                stop = calm_since + window + 2 * resources.RECOVERY_MAX_GAP
                for second in range(calm_since, stop + 1):
                    now[0] = float(second)
                    tick()
                    if len(starts) == 2:
                        break
                latch = json.loads((lane / 'resource-recovery.json').read_text())
                emit({'case': 'retry-admission-latch', 'load': load, 'window': window,
                      'starts': starts, 'attests': attests, 'admission': observations,
                      'retryAt': worker.retry_at, 'state': worker.state,
                      'failures': worker.failures, 'latch': latch})
                self.assertEqual(len(starts), 2, 'tick retry must not reset the real recovery window forever')
                ready_window = [row for row in observations
                                if row['at'] >= calm_since and row['reason'] == 'resource_recovery_hysteresis']
                self.assertTrue(ready_window, 'the paused real latch must observe a stable window')
                self.assertGreaterEqual(starts[1] - ready_window[0]['at'], window)
                self.assertFalse(latch['paused'])
                self.assertEqual((worker.state, worker.failures), ('ready', 0))
                self.assertEqual(len(attests), 2, 'resource deferrals must not re-attest or count as failures')

    def test_hosted_ensure_preserves_normal_startup_replacement_and_matching_paths(self):
        for case in ('absent', 'learning-starting', 'host-replacing', 'python-switch', 'matched'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='rt-wait-', dir='/tmp') as temp:
                base = Path(temp).resolve()
                root, service_root = base / 'learning', base / 'service'
                root.mkdir(mode=0o700)
                service_root.mkdir(mode=0o700)
                now, reads = [100.0], []
                sleeps, drains, spawns, states = [], [], [], []
                good = {'status': 'ready', 'hosted': True, 'protocol': learning.PROTOCOL,
                        'signature': 'same-signature', 'python': sys.executable}
                initial = None if case == 'absent' else dict(good)
                if case == 'learning-starting':
                    initial.update(status='starting', signature='previous-signature')
                if case == 'host-replacing':
                    initial['signature'] = 'previous-signature'
                if case == 'python-switch':
                    initial['python'] = '/synthetic/other-python'
                hello = None if case == 'absent' else {
                    'status': 'draining' if case == 'host-replacing' else 'ready',
                    'protocols': [host.PROTOCOL],
                    'bundle': {'version': '1.0.0', 'digest': 'same-bundle'},
                    'python': '/synthetic/other-python' if case == 'python-switch' else sys.executable}
                def version(*_args):
                    reads.append(now[0])
                    return dict(good) if case == 'matched' or len(reads) >= 3 else initial
                def sleep(seconds):
                    sleeps.append(seconds)
                    now[0] += seconds
                def ensure(**kwargs):
                    state = REAL_ENSURE(root=service_root, legacy_root=None, **kwargs)
                    states.append(state)
                    return state
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.dict(os.environ, {'HOME': str(base),
                        'QMD_SETUP_GUARD_FIXTURE': '1', 'MEMVANE_TEST_RUNTIME_ROOT': str(base)}))
                    stack.enter_context(mock.patch.object(learning, '_signature', return_value='same-signature'))
                    stack.enter_context(mock.patch.object(learning, '_version', version))
                    stack.enter_context(mock.patch.object(learning.time, 'monotonic', lambda: now[0]))
                    stack.enter_context(mock.patch.object(learning.time, 'sleep', sleep))
                    stack.enter_context(mock.patch.object(host, 'bundle_identity',
                        return_value={'version': '1.0.0', 'digest': 'same-bundle'}))
                    stack.enter_context(mock.patch.object(host, 'hello', return_value=hello))
                    stack.enter_context(mock.patch.object(host, '_lease_held', return_value=True))
                    stack.enter_context(mock.patch.object(host, 'exchange',
                        side_effect=lambda *a, **k: drains.append(a) or {'status': 'draining'}))
                    stack.enter_context(mock.patch.object(host, '_spawn',
                        side_effect=lambda *a, **k: spawns.append(a)))
                    stack.enter_context(mock.patch.object(host, 'ensure_service', side_effect=ensure))
                    learning._ensure(root, sys.executable, now[0] + 1)
                emit({'case': case, 'hostStates': states, 'sleeps': sleeps,
                      'drains': len(drains), 'spawns': len(spawns), 'versionReads': len(reads)})
                if case == 'matched':
                    self.assertEqual((states, sleeps, drains, spawns), ([], [], [], []))
                else:
                    self.assertTrue(sleeps, 'a genuine transition must still wait')
                    self.assertEqual(states, ['replacing' if case in ('host-replacing', 'python-switch')
                                              else 'starting' if case == 'absent' else 'ready'])
                    self.assertEqual(len(spawns), int(case in ('host-replacing', 'python-switch')))
                    self.assertEqual(len(drains), int(case == 'python-switch'))

    def test_host_protocol_mismatch_keeps_existing_wire_code_and_selector_fallback_reason(self):
        from context_learning import live_select
        with tempfile.TemporaryDirectory(prefix='rt-protocol-', dir='/tmp') as temp:
            base = Path(temp).resolve()
            root, service_root = base / 'learning', base / 'service'
            root.mkdir(mode=0o700)
            service_root.mkdir(mode=0o700)
            def ensure(**kwargs):
                return REAL_ENSURE(root=service_root, legacy_root=None, **kwargs)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(learning, '_version', return_value=None))
                stack.enter_context(mock.patch.object(host, 'hello', return_value={
                    'status': 'ready', 'protocols': [], 'python': sys.executable,
                    'bundle': {'version': '2.0.0', 'digest': 'synthetic-new'}}))
                stack.enter_context(mock.patch.object(host, 'bundle_identity',
                    return_value={'version': '1.0.0', 'digest': 'synthetic-old'}))
                stack.enter_context(mock.patch.object(host, 'ensure_service', side_effect=ensure))
                stack.enter_context(mock.patch.object(host, '_spawn',
                    side_effect=AssertionError('protocol mismatch must not spawn')))
                stack.enter_context(mock.patch.object(host, 'exchange',
                    side_effect=AssertionError('protocol mismatch must not drain')))
                with self.assertRaises(learning.ServiceFailure) as observed:
                    learning._ensure(root, sys.executable, learning.time.monotonic() + 1)
            failure = observed.exception
            self.assertEqual(failure.code, 'version_mismatch')
            self.assertEqual(failure.timing, {'host_reason': 'laya_service_protocol_mismatch'})
            self.assertEqual(learning._failure(failure), 'version_mismatch')
            # This checks the actual choose exception-normalization boundary;
            # the model/prediction front end is deliberately not exercised.
            with mock.patch.object(live_select, 'read_private_json', side_effect=failure):
                ids, reason, resources_out = live_select.choose(root, 'synthetic prompt',
                    [{'id': 'synthetic'}], corpus_fingerprint='a' * 64)
            emit({'case': 'host-protocol-selector-fallback', 'wireCode': learning._failure(failure),
                  'selectorReason': reason, 'resources': resources_out})
            self.assertIsNone(ids)
            self.assertEqual(reason, 'resident_service_version_mismatch')
            self.assertEqual(resources_out['host_reason'], 'laya_service_protocol_mismatch')

    def test_malformed_and_unsafe_learning_observations_never_become_success(self):
        for kind in ('malformed', 'unsafe'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='rt-invalid-', dir='/tmp') as temp:
                root = Path(temp).resolve()
                now, calls = [100.0], []
                def version(*_args):
                    calls.append(True)
                    if kind == 'unsafe':
                        raise learning.ServiceFailure('unsafe_socket')
                    return {'status': 'ready', 'hosted': True, 'protocol': learning.PROTOCOL,
                            'signature': 123, 'python': sys.executable}
                def sleep(seconds):
                    now[0] += seconds
                with mock.patch.object(learning, '_version', version), \
                        mock.patch.object(learning, '_signature', return_value='same-signature'), \
                        mock.patch.object(host, 'ensure_service', return_value='ready'), \
                        mock.patch.object(learning.time, 'monotonic', lambda: now[0]), \
                        mock.patch.object(learning.time, 'sleep', sleep):
                    with self.assertRaises(learning.ServiceFailure) as observed:
                        learning._ensure(root, sys.executable, now[0] + .1)
                self.assertEqual(observed.exception.code, 'starting')
                self.assertGreater(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
