"""The fixed wake handler table matches the runtime's wake kinds; importing it loads no learning code."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from memvane.app import wiring
from memvane.runtime import api as runtime

ROOT = Path(__file__).resolve().parents[4]


class HandlerTable(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='mvh-')))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)

    def test_every_handler_kind_is_a_runtime_wake_kind_and_no_other_kind_dispatches(self):
        self.assertEqual(sorted(wiring.HANDLERS), ['auto', 'laya-compile', 'laya-sync', 'learning'])
        queue = runtime.wake_queue(self.root)
        for kind, name in wiring.HANDLERS.items():
            with self.subTest(kind=kind):
                self.assertTrue(callable(getattr(wiring, name)))
                self.assertEqual(queue.offer(kind, '/synthetic/project'), 'queued')
        for kind in ('index', 'update', 'cleanup', 'arbitrary-command', ''):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    queue.offer(kind, '/synthetic/project')
                with self.assertRaises(ValueError) as caught:
                    wiring.dispatch({'kind': kind, 'target': '/synthetic/project'})
                self.assertEqual(str(caught.exception), 'invalid_runtime_handler')

    def test_importing_the_handlers_loads_no_learning_code(self):
        # A fresh interpreter: sibling tests already import learning modules.
        probe = ('import sys;import memvane.app.wiring;'
                 "print(sorted(m for m in sys.modules if m.startswith('memvane.learning')))")
        result = subprocess.run([sys.executable, '-B', '-c', probe], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')


if __name__ == '__main__':
    unittest.main()
