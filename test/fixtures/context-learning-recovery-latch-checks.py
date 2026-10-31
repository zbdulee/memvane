"""Resource recovery latch: a busy but admissible machine is not paused forever."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from context_learning import resource_coordination as rc


class LatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.clock = [1000.0]
        self.c = rc.Coordinator(self.root, clock=lambda: self.clock[0],
                                wall=lambda: self.clock[0] + 5e8)
        self.policy = {'maxRssMib': 2048, 'minAvailableMib': 768, 'maxLoadPerCpu': 1}

    def report(self, load):
        return {'ready': True, 'total_mib': 24576, 'available_mib': 5000, 'load_per_cpu': load,
                'observed_at_mono': self.clock[0], 'query_status': {'memory_pressure': 'ok'},
                'memory_pressure': {'system_free_percent': 40, 'level': 'normal'}}

    def drive(self, load, seconds, step=5):
        ready = []
        for _ in range(int(seconds / step) + 1):
            ready.append(self.c.check(self.policy, self.report(load))['ready'])
            self.clock[0] += step
        return ready

    def pause(self):
        self.assertFalse(self.c.check(self.policy, self.report(1.2))['ready'])
        self.assertTrue(json.loads((self.root / 'resource-recovery.json').read_text())['paused'])

    def test_steady_busy_but_admissible_load_recovers_after_the_long_window(self):
        self.pause()
        ready = self.drive(0.9, 70)
        self.assertTrue(any(ready), 'latched paused forever at 0.9 load per cpu')
        first = ready.index(True)
        self.assertGreaterEqual(first * 5, rc.RECOVERY_BUSY_SECONDS)
        self.assertFalse(json.loads((self.root / 'resource-recovery.json').read_text())['paused'])

    def test_calm_load_still_recovers_after_the_short_window(self):
        self.pause()
        ready = self.drive(0.3, 15)
        self.assertEqual(ready.index(True) * 5, 5)

    def test_a_refusal_or_a_gap_restarts_the_window(self):
        self.pause()
        self.drive(0.9, 40)
        self.pause()                       # over the hard limit again: start over
        self.assertFalse(any(self.drive(0.9, 40)))
        self.clock[0] += 30                # a gap longer than RECOVERY_MAX_GAP
        self.assertFalse(any(self.drive(0.9, 40)))


if __name__ == '__main__':
    unittest.main()
