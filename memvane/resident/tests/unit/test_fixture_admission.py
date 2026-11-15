"""S5 L0 R1: query admission asks a fixture provider port only under a test runtime root."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from memvane.kernel.resource_deferred import ResourceDeferred
from memvane.provider.types import FIXTURE_FILE
from memvane.resident import api as resident


class _Coordinator:
    def __init__(self):
        self.checks = 0

    def check(self, policy, report):
        self.checks += 1
        return {'ready': True}


class FixtureAdmission(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / FIXTURE_FILE).write_text(json.dumps(
            {'schema': 'memvane-test-provider-fixture-v1', 'dimension': 2}))
        os.chmod(self.root / FIXTURE_FILE, 0o600)

    def environ(self, root):
        env = {k: v for k, v in os.environ.items() if k != 'MEMVANE_TEST_RUNTIME_ROOT'}
        if root is not None:
            env['MEMVANE_TEST_RUNTIME_ROOT'] = root
        return mock.patch.dict(os.environ, env, clear=True)

    def test_without_a_test_root_the_runtime_policy_admits(self):
        coordinator = _Coordinator()
        with self.environ(None):
            self.assertIsNone(resident.admit_query(coordinator=coordinator, report={}))
        self.assertEqual(coordinator.checks, 1)

    def test_a_test_root_fixture_port_answers_instead(self):
        coordinator = _Coordinator()
        with self.environ(str(self.root)), self.assertRaises(ResourceDeferred) as caught:
            resident.admit_query(coordinator=coordinator, report={})
        self.assertEqual((str(caught.exception), coordinator.checks), ('fixture_lexical_recall', 0))


if __name__ == '__main__':
    unittest.main()
