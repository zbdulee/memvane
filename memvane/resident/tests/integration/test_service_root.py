"""The resident service ends when its service root or lease file goes away."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from memvane.resident import resident_service as svc


class _Handle:
    """Owned worker handle contract: ``.process`` and ``stop()``."""

    def __init__(self):
        self.process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                        stdin=subprocess.PIPE)

    def stop(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


class ServiceRootLifetime(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvr-', dir='/tmp')))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = self.base / 'svc'
        self.root.mkdir(mode=0o700)
        self.base.chmod(0o700)

    def running(self):
        """A service holding its lease with one loaded worker, run in a thread."""
        service = svc.Service(self.root, bundle={'version': '1.0.0', 'digest': 'a'},
                              prove=lambda project: None, attest=lambda *_a: None,
                              start_worker=lambda *_a: None, admit=lambda: True, tick=.05)
        service.reclaim_seconds = 3600
        self.assertTrue(service.acquire(wait=5))
        worker = svc.Worker(('synthetic',), str(self.base / 'worker.sock'))
        worker.handle, worker.state = _Handle(), 'ready'
        service.workers['synthetic'] = worker
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (service.stopped.set(), thread.join(10)))
        self.addCleanup(lambda: worker.handle.process.poll() is None and worker.handle.process.kill())
        # Many ticks pass while the root is intact: the service keeps serving.
        self.assertFalse(service.stopped.wait(.5))
        self.assertTrue(worker.alive())
        return service, thread, worker.handle.process

    def assert_ended(self, service, thread, process):
        self.assertTrue(service.stopped.wait(30), 'service kept running')
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(process.poll(), 'worker kept running')
        self.assertEqual(service.workers, {})
        self.assertIsNone(service.lease)
        self.assertTrue(service.draining.is_set())

    def test_deleted_state_base_drains_the_worker_and_stops(self):
        service, thread, process = self.running()
        shutil.rmtree(self.root)
        self.assert_ended(service, thread, process)

    def test_replaced_service_root_drains_the_worker_and_stops(self):
        service, thread, process = self.running()
        self.root.rename(self.base / 'old-svc')
        self.root.mkdir(mode=0o700)
        self.assert_ended(service, thread, process)

    def test_replaced_lease_file_drains_the_worker_and_stops(self):
        service, thread, process = self.running()
        lease = self.root / svc.LEASE
        lease.rename(self.root / 'old.lock')
        lease.touch(mode=0o600)
        self.assert_ended(service, thread, process)


if __name__ == '__main__':
    unittest.main()
