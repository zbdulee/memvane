"""S5 L0 R1: the fixture provider port seam is inert without a test runtime root."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memvane.kernel.errors import IndexContractError
from memvane.provider import api as provider
from memvane.provider.types import FIXTURE_FILE

SPEC = {'schema': 'memvane-test-provider-fixture-v1', 'dimension': 3, 'identityChanged': False}


class _Port:
    def __init__(self):
        self.calls = []

    def reviewed_provider(self, manifest):
        self.calls.append(('reviewed_provider', manifest))
        return 'fixture-handle'

    def sealed_files_sha256(self, path):
        self.calls.append(('sealed_files_sha256', path))
        return 'd' * 64


class FixturePortSeam(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root, ignore_errors=True))
        os.chmod(self.root, 0o700)

    def env(self, value):
        environ = {key: item for key, item in os.environ.items() if key != 'MEMVANE_TEST_RUNTIME_ROOT'}
        if value is not None:
            environ['MEMVANE_TEST_RUNTIME_ROOT'] = value
        return mock.patch.dict(os.environ, environ, clear=True)

    def write_spec(self, mode=0o600, spec=SPEC):
        path = self.root / FIXTURE_FILE
        path.write_text(json.dumps(spec))
        os.chmod(path, mode)
        return path

    def test_without_a_test_root_every_call_is_production(self):
        self.write_spec()
        for value in (None, '', 'relative/root'):
            with self.env(value):
                self.assertIsNone(provider.fixture_port())
                # The real provider validates the manifest itself: no stand-in answered.
                with self.assertRaises(IndexContractError):
                    provider.reviewed_provider({})
                self.assertIsNone(provider.sealed_files_sha256(str(self.root / 'not-a-generation')))

    def test_a_test_root_without_the_fixture_file_is_production(self):
        with self.env(str(self.root)):
            self.assertIsNone(provider.fixture_port())
            with self.assertRaises(IndexContractError):
                provider.reviewed_provider({})

    def test_an_explicit_port_wins_without_any_test_root(self):
        port = _Port()
        with self.env(None):
            self.assertIs(provider.fixture_port(port), port)
            self.assertEqual(provider.reviewed_provider({'m': 1}, port=port), 'fixture-handle')
            self.assertEqual(provider.sealed_files_sha256('/x', port=port), 'd' * 64)
        self.assertEqual(port.calls, [('reviewed_provider', {'m': 1}), ('sealed_files_sha256', '/x')])

    def test_the_test_root_file_builds_the_fixture_port(self):
        self.write_spec()
        with self.env(str(self.root)):
            port = provider.fixture_port()
            self.assertEqual((type(port).__name__, port.dimension), ('FixtureProvider', 3))
            handle = provider.reviewed_provider({'k': 'v'})
            self.assertEqual((handle.manifest, handle.embed_document('body')), ({'k': 'v'}, [1.0, 0.0, 0.0]))
            self.assertEqual(provider.sealed_files_sha256('/anything'), 'c' * 64)

    def test_an_unsafe_fixture_file_refuses(self):
        self.write_spec(mode=0o644)
        with self.env(str(self.root)), self.assertRaises(ValueError) as caught:
            provider.fixture_port()
        self.assertEqual(str(caught.exception), 'provider_fixture_unsafe')
        os.unlink(self.root / FIXTURE_FILE)
        target = self.root / 'elsewhere.json'
        target.write_text(json.dumps(SPEC))
        os.chmod(target, 0o600)
        os.symlink(target, self.root / FIXTURE_FILE)
        with self.env(str(self.root)), self.assertRaises(OSError):
            provider.fixture_port()

    def test_a_foreign_spec_refuses(self):
        self.write_spec(spec={'schema': 'other', 'dimension': 2})
        with self.env(str(self.root)), self.assertRaises(ValueError) as caught:
            provider.fixture_port()
        self.assertEqual(str(caught.exception), 'provider_fixture_invalid')


if __name__ == '__main__':
    unittest.main()
