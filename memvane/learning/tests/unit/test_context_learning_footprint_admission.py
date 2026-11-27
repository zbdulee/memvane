"""First-load admission uses the measured footprint, not the RSS ceiling."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memvane.learning.context_learning import inference_service, resident, selector_heal


def probe(available):
    return lambda policy, detailed=True: {
        'ready': True, 'available_mib': available, 'load_per_cpu': .2,
        'memory_pressure': {'system_free_percent': 34, 'level': 'warning'}}


class AdmissionTests(unittest.TestCase):
    def test_first_load_is_not_held_to_the_rss_ceiling(self):
        # 24 GiB host with ~3 GiB available: the old max(2048, maxRssMib=4096)
        # floor deferred every release's preparation.
        limits, _ = resident._admission(4096, already_running=False, probe=probe(3000))
        self.assertIsNotNone(limits, 'first load deferred below the 4096 RSS ceiling')
        self.assertEqual(resident.estimated_peak_mib(None) + resident.LOAD_RESERVE_MIB, 2304)

    def test_a_recorded_heavy_footprint_still_defers(self):
        limits, _ = resident._admission(4096, already_running=False, probe=probe(3000),
                                        peak_mib=2000)   # 1.5x = 3000 + 768 reserve
        self.assertIsNone(limits)
        limits, _ = resident._admission(4096, already_running=False, probe=probe(3800),
                                        peak_mib=2000)
        self.assertIsNotNone(limits)

    def test_estimate_has_a_floor_and_a_default(self):
        self.assertEqual(resident.estimated_peak_mib(100), resident.MIN_PEAK_MIB)
        self.assertEqual(resident.estimated_peak_mib(None), resident.DEFAULT_PEAK_MIB)
        self.assertEqual(resident.estimated_peak_mib(float('nan')), resident.DEFAULT_PEAK_MIB)
        self.assertEqual(resident.estimated_peak_mib(1000), 1500)
        limits, _ = resident._admission(4096, already_running=False, probe=probe(1500),
                                        peak_mib=10)    # floor 1024 + 768 > 1500
        self.assertIsNone(limits)


class FootprintRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name).resolve(); self.state.chmod(0o700)

    def test_record_is_bound_to_model_and_view(self):
        self.assertIsNone(resident.load_footprint(self.state, '/m', 'v'))
        resident.record_footprint(self.state, '/m', 'v', 373.2)
        self.assertEqual(resident.load_footprint(self.state, '/m', 'v'), 374)
        self.assertIsNone(resident.load_footprint(self.state, '/other', 'v'))
        self.assertIsNone(resident.load_footprint(self.state, '/m', 'other-view'))
        resident.record_footprint(self.state, '/m', 'v', 0)   # never records a non-measurement
        self.assertEqual(resident.load_footprint(self.state, '/m', 'v'), 374)

    def test_heal_pressure_and_the_service_job_use_the_record(self):
        seen = []
        def admission(max_rss, *, already_running, peak_mib=None, probe=None):
            seen.append(peak_mib); return None, {}
        (self.state / 'active-checkpoint.json').write_text(json.dumps({'schema_version': 1}))
        (self.state / 'active-checkpoint.json').chmod(0o600)
        resident.record_footprint(self.state, '/m', 'checkpoint-config-v1', 600)
        with patch.object(resident, '_admission', side_effect=admission):
            self.assertEqual(selector_heal.pressure({'maxRssMib': 4096, 'trainerArgv': ['p', 'a', '/m']},
                                                    self.state), 'learning_prepare_memory_pressure')
        engine = inference_service.GenerationEngine(load=lambda job: 'm', forward=lambda *a, **k: {},
                                                    admission=admission, rss=lambda: 100)
        job = {'family': ('r', 'b', 'v'), 'generation': ('r', 'b', 'v', 'h'), 'namespace': 'n',
               'policy': {'maxRssMib': 4096}, 'request': {}, 'deadline': time.monotonic() + 5,
               'peak_mib': 600}
        with self.assertRaisesRegex(inference_service.ServiceFailure, 'resource_deferred'):
            engine.run(job, cancelled=lambda: False, revalidate=lambda: job)
        self.assertEqual(seen, [600, 600])



class TrainerFootprintTests(unittest.TestCase):
    """Training admission: the measured trainer peak, not the 4,096 MiB RSS ceiling."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.policy = {'trainerArgv': [sys.executable, '/fixture/adapter.py', '/fixture/model'],
                       'maxRssMib': 4096, 'minAvailableMib': 1024, 'maxLoadPerCpu': .8}

    def test_a_measured_peak_admits_on_its_envelope_under_the_ceiling(self):
        from memvane.learning.context_learning import trainer_footprint as footprint
        from memvane.runtime.api import admission_policy
        self.assertIs(footprint.admission_view(self.root, self.policy), self.policy)  # nothing measured yet
        # About 1.9 GB measured over a real training run (1,945 MiB).
        footprint.record(self.root, self.policy, {'observed_peak_rss_kib': 1945 * 1024, 'resource_samples': 40})
        view = footprint.admission_view(self.root, self.policy)
        self.assertEqual(view['maxRssMib'], 2918)  # max(ceil(1.5 * 1945), 1945 + 512)
        self.assertEqual(admission_policy(view)['maxRssMib'] + view['minAvailableMib'], 3942)
        self.assertEqual(self.policy['maxRssMib'], 4096)  # the running trainer's ceiling is unchanged
        # A smaller later run never lowers it; a larger one raises it; the ceiling caps it.
        footprint.record(self.root, self.policy, {'observed_peak_rss_kib': 1000 * 1024, 'resource_samples': 40})
        self.assertEqual(footprint.admission_view(self.root, self.policy)['maxRssMib'], 2918)
        footprint.record(self.root, self.policy, {'observed_peak_rss_kib': 3000 * 1024, 'resource_samples': 40})
        self.assertEqual(footprint.admission_view(self.root, self.policy)['maxRssMib'], 4096)

    def test_what_is_not_a_measurement_keeps_the_ceiling(self):
        from memvane.learning.context_learning import trainer_footprint as footprint
        for resources in ({'recovered_completion': True}, {'observed_peak_rss_kib': None},
                          {'observed_peak_rss_kib': 1945 * 1024, 'resource_samples': 1},
                          {'observed_peak_rss_kib': -1, 'resource_samples': 40}):
            footprint.record(self.root, self.policy, resources)
        self.assertFalse((self.root / footprint.NAME).exists())
        # Another trainer command, an explicit memory profile, an unreadable record.
        footprint.record(self.root, self.policy, {'observed_peak_rss_kib': 1945 * 1024, 'resource_samples': 40})
        other = {**self.policy, 'trainerArgv': [sys.executable, '/fixture/adapter.py', '/other/model']}
        self.assertIs(footprint.admission_view(self.root, other), other)
        adaptive = {**self.policy, 'adaptiveMemory': {'fixedMib': 1}}
        self.assertIs(footprint.admission_view(self.root, adaptive), adaptive)
        (self.root / footprint.NAME).write_text('{not json')
        self.assertIs(footprint.admission_view(self.root, self.policy), self.policy)
        self.assertEqual(footprint.envelope(100), footprint.MIN_ENVELOPE_MIB)


class IdleReleaseTests(unittest.TestCase):
    """A loaded selector model with no request for a while is released; the service stays."""

    def engine(self):
        loads = []
        engine = inference_service.GenerationEngine(
            load=lambda job: loads.append(job['generation']) or object(),
            forward=lambda *_a, **_k: {'schema_version': 1, 'predictions': []},
            admission=lambda *_a, **_k: ({'batch': 1}, {'available_mib': 8000}), rss=lambda: 100,
            end_reuse=lambda: None)
        # A fixed clock: ``(last + IDLE_RELEASE_SECONDS) - last`` on an uptime-sized monotonic
        # value can round one ULP below the limit and make the boundary host-dependent.
        engine.last_used = 1000.0
        return engine, loads

    def test_an_idle_model_is_released_and_the_next_request_loads_it_again(self):
        from memvane.learning.context_learning import idle_release
        engine, loads = self.engine()
        engine.active, engine.footprint_mib = (('generation',), object()), 512
        last = engine.last_used
        self.assertFalse(idle_release.release_idle(engine, now=last + idle_release.IDLE_RELEASE_SECONDS - 1))
        self.assertIsNotNone(engine.active)
        self.assertTrue(idle_release.release_idle(engine, now=last + idle_release.IDLE_RELEASE_SECONDS))
        self.assertEqual((engine.active, engine.footprint_mib, engine.cached_load), (None, None, None))
        self.assertFalse(idle_release.release_idle(engine, now=last + 2 * idle_release.IDLE_RELEASE_SECONDS))

    def test_a_prepared_generation_waiting_for_its_activation_is_kept(self):
        from memvane.learning.context_learning import idle_release
        engine, _loads = self.engine()
        engine.active = (('generation',), object())
        later = engine.last_used + idle_release.IDLE_RELEASE_SECONDS
        engine.pending = ('token', 'key', engine.active, 512, later + 60)
        self.assertFalse(idle_release.release_idle(engine, now=later))
        # Once its deadline passed nothing can activate it: both go.
        self.assertTrue(idle_release.release_idle(engine, now=later + 60))
        self.assertEqual((engine.active, engine.pending), (None, None))

    def test_a_request_counts_as_use(self):
        from memvane.learning.context_learning import idle_release
        engine, _loads = self.engine()
        engine.active = (('generation',), object())
        engine.last_used -= idle_release.IDLE_RELEASE_SECONDS
        with self.assertRaises(Exception):  # the double's request ends early; it was still a request
            engine.run({'deadline': 0}, cancelled=lambda: False, revalidate=lambda: None)
        self.assertFalse(idle_release.release_idle(engine))
        self.assertFalse(idle_release.release_idle(object()))  # an engine without the clock keeps its model

    def test_an_expired_pending_generation_on_a_cold_engine_is_released(self):
        from memvane.learning.context_learning import idle_release
        engine, _loads = self.engine()
        last = engine.last_used
        engine.pending = ('token', 'key', (('cand',), object()), 512, last + 60)
        self.assertFalse(idle_release.release_idle(engine, now=last + 59))
        self.assertIsNotNone(engine.pending)
        self.assertTrue(idle_release.release_idle(engine, now=last + 60))
        self.assertIsNone(engine.pending)
        self.assertFalse(idle_release.release_idle(engine, now=last + 61))


if __name__ == '__main__':
    unittest.main()
