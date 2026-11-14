"""A hung cold load is reaped before the successor acquires the service lease."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from memvane.resident import resident_service as svc
from tests.support.case import FixtureCase


class ForcedExit(FixtureCase):
    def test_hung_load_reaps_owned_child_and_successor_takes_lease(self):
        root = self.home / 'service'
        root.mkdir(mode=0o700)
        driver = subprocess.Popen(
            [sys.executable, '-B', '-m', 'memvane.resident.tests.hung_driver', str(self.home)],
            env={**os.environ, 'LAYA_SERVICE_TIME_SCALE': '0.02'},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        (self.home / 'driver.pid').write_text(str(driver.pid))
        child_record = self.home / 'child.pid'

        def cleanup():
            if driver.poll() is None:
                driver.kill()
            driver.wait(timeout=10)
            if child_record.exists():
                pid = int(child_record.read_text())
                try:
                    if os.getpgid(pid) == driver.pid:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup)
        self.wait_until(lambda: (self.home / 'draining').exists(), timeout=20)
        successor = svc.Service(root, bundle={'version': '1.0.1'})
        self.addCleanup(successor.close)
        self.assertTrue(successor.acquire(wait=15))
        self.assertEqual(driver.wait(timeout=15), svc.FORCED_EXIT_CODE)
        child = int(child_record.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        record = json.loads((root / svc.FORCED_EXIT).read_text())
        self.assertEqual((record['reason'], record['unconfirmedChildren']), ('cold_load_hung', []))
        self.assertIn(child, record['killedChildren'])

    def test_drain_budget_leaves_successor_time_to_acquire(self):
        from unittest.mock import patch
        self.assertLessEqual(svc.DRAIN_SECONDS, svc.DRAIN_BUDGET_SECONDS)
        budget = (svc._scaled(svc.DRAIN_BUDGET_SECONDS) + svc._exit_cleanup_seconds()
                  + svc._scaled(svc.DRAIN_SAFETY_SECONDS))
        self.assertLessEqual(budget, svc.SUCCESSOR_WAIT_SECONDS)
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '1',
                                     'LAYA_SERVICE_TIME_SCALE': '0.02'}):
            budget = (svc._scaled(svc.DRAIN_BUDGET_SECONDS) + svc._exit_cleanup_seconds()
                      + svc._scaled(svc.DRAIN_SAFETY_SECONDS))
            self.assertLessEqual(budget, svc._scaled(svc.SUCCESSOR_WAIT_SECONDS))
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': '',
                                     'LAYA_SERVICE_TIME_SCALE': '0.01'}):
            self.assertEqual(svc._scaled(svc.DRAIN_BUDGET_SECONDS), svc.DRAIN_BUDGET_SECONDS)
