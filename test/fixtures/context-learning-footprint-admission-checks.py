"""First-load admission uses the measured footprint, not the RSS ceiling."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import inference_service, resident, selector_heal


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


if __name__ == '__main__':
    unittest.main()
