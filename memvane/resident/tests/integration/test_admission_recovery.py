"""Resident start retry follows the real public resource admission gate."""
from types import SimpleNamespace
from unittest.mock import patch

from memvane.kernel.errors import IndexContractError
from memvane.resident import embed_resident as owner
from memvane.resident import resident_service as service_module
from memvane.resident.tests.support import Fixture
from memvane.runtime import api as runtime_api


class InlineThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class AdmissionRecovery(Fixture):
    def test_failed_start_then_pressure_recovers_through_real_admission_windows(self):
        for load in (0.2, 0.8):
            with self.subTest(load=load):
                project, manifest = self.project('p' + str(load))
                decision, proof, mode = self.prove(project)
                lane = self.home / ('lane' + str(load))
                lane.mkdir(mode=0o700)
                now, phase = [100.0], ['calm']
                controller = runtime_api.coordinator(lane, clock=lambda: now[0],
                                                     wall=lambda: 1000 + now[0])
                policy = {'maxLoadPerCpu': 1, 'minAvailableMib': 256,
                          'maxRssMib': 512, 'targetHeadroomFraction': 0.35}
                observations, starts, attests = [], [], []

                def admit():
                    report = {'ready': True, 'observed_at_mono': now[0],
                              'total_mib': 16384, 'available_mib': 8192,
                              'load_per_cpu': 2.0 if phase[0] == 'pressure' else load,
                              'swap_used_mib': 0, 'query_status': {'memory_pressure': 'ok'},
                              'memory_pressure': {'level': 'normal', 'system_free_percent': 80}}
                    result = controller.check(policy, report)
                    observations.append(result['reason'])
                    return result['ready']

                def start(_manifest, _admit):
                    starts.append(now[0])
                    if len(starts) == 1:
                        raise IndexContractError('synthetic_start_failure')
                    return SimpleNamespace(process=SimpleNamespace(poll=lambda: None), stop=lambda: None)

                service = service_module.Service(self.root, bundle={'version': '1.0.0'},
                    prove=self.prove, attest=lambda *_args: attests.append(now[0]),
                    start_worker=start, admit=admit)
                key = service_module._worker_key(manifest)
                with patch.object(service_module.time, 'monotonic', lambda: now[0]), \
                     patch.object(owner.time, 'time', lambda: 1000 + now[0]), \
                     patch.object(service_module.threading, 'Thread', InlineThread):
                    def tick():
                        service._load(project, decision, proof, mode, manifest, key, 'seal')
                    tick()
                    worker = service.workers[key]
                    self.assertEqual((len(starts), worker.failures, worker.state), (1, 1, 'failed'))
                    now[0], phase[0] = worker.retry_at, 'pressure'
                    tick()
                    self.assertEqual((len(starts), worker.failures, worker.state),
                                     (1, 1, 'resource_deferred'))
                    phase[0] = 'calm'
                    for second in range(int(now[0]) + 1, int(now[0]) + 100):
                        now[0] = float(second)
                        tick()
                        if len(starts) == 2:
                            break
                self.assertIn('resource_recovery_hysteresis', observations)
                self.assertEqual((len(starts), len(attests), worker.state, worker.failures),
                                 (2, 2, 'ready', 0))
