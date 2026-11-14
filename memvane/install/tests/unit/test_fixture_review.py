"""S5 L0 R1: provider review asks an explicit fixture port; without one production reviews."""
import os
import unittest
from unittest import mock

from memvane.install import api as install
from memvane.install.types import InstallBlocked


class _Port:
    def __init__(self):
        self.calls = []

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return {'status': 'review_required', 'proposalSha256': '9' * 64}


class FixtureReview(unittest.TestCase):
    def environ(self):
        env = {k: v for k, v in os.environ.items() if k != 'MEMVANE_TEST_RUNTIME_ROOT'}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_an_explicit_port_reviews(self):
        port = _Port()
        with self.environ():
            result = install.review_local_provider(plugin_root='relative', state_root='relative',
                                                   dimension=2, provider=port)
        self.assertEqual(result['proposalSha256'], '9' * 64)
        self.assertEqual(port.calls, [{'plugin_root': 'relative', 'state_root': 'relative',
                                       'model_path': None, 'dimension': 2, 'runtime_generation': None}])

    def test_without_a_port_or_test_root_production_reviews(self):
        with self.environ(), mock.patch('platform.system', return_value='Darwin'), \
                mock.patch('platform.machine', return_value='arm64'):
            with self.assertRaises(InstallBlocked) as caught:
                install.review_local_provider(plugin_root='relative', state_root='relative', dimension=2)
        self.assertEqual(str(caught.exception), 'provider_review_paths_invalid')


if __name__ == '__main__':
    unittest.main()
