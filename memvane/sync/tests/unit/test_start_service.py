"""S5 L0 R2: ``sync.api.run`` takes the warm-service starter; the default stays the installed one."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.provider.api import start_owned_embed_service
from memvane.provider.types import FIXTURE_FILE
from memvane.sync import api as sync
from memvane.sync import product_sync_worker as worker


class StartService(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.seen = []
        stack = mock.patch.object(worker, '_run_sync', self.record)
        stack.start()
        self.addCleanup(stack.stop)
        lookup = mock.patch('memvane.install.api.build_lookup', return_value=None)
        lookup.start()
        self.addCleanup(lookup.stop)

    def record(self, project, *, coordinator=None, report=None, start_service=None):
        self.seen.append(start_service)
        return {'state': 'unchanged'}

    def environ(self, root):
        env = {k: v for k, v in os.environ.items() if k != 'MEMVANE_TEST_RUNTIME_ROOT'}
        if root is not None:
            env['MEMVANE_TEST_RUNTIME_ROOT'] = root
        return mock.patch.dict(os.environ, env, clear=True)

    def test_an_explicit_starter_is_used(self):
        starter = lambda manifest, *, admit=None: None
        with self.environ(None):
            self.assertEqual(sync.run(self.root, start_service=starter), {'state': 'unchanged'})
        self.assertEqual(self.seen, [starter])

    def test_without_a_starter_or_test_root_the_installed_service_starts(self):
        with self.environ(None):
            sync.run(self.root)
        with self.environ(str(self.root)):  # a test root without a fixture file
            sync.run(self.root)
        self.assertEqual(self.seen, [start_owned_embed_service] * 2)

    def test_a_test_root_fixture_port_starts_its_service(self):
        (self.root / FIXTURE_FILE).write_text(json.dumps(
            {'schema': 'memvane-test-provider-fixture-v1', 'dimension': 2}))
        os.chmod(self.root / FIXTURE_FILE, 0o600)
        with self.environ(str(self.root)):
            sync.run(self.root)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(getattr(self.seen[0], '__name__', None), 'start_service')
        self.assertIsNot(self.seen[0], start_owned_embed_service)
        self.seen[0]({}, admit=None).stop()


if __name__ == '__main__':
    unittest.main()
