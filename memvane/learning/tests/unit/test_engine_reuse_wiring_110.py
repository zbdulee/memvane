"""The inference engine's default wiring ends the identity reuse window of the service's own cache."""
import unittest
from unittest import mock

from memvane.learning.context_learning import inference_service as service


class DefaultWiring(unittest.TestCase):
    def engine(self, **kwargs):
        return service.GenerationEngine(load=lambda job: object(), forward=lambda *a, **k: {},
                                        admission=lambda *a, **k: ({}, {}), rss=lambda: 1, **kwargs)

    def test_an_engine_built_without_the_argument_ends_the_reuse_of_the_module_cache(self):
        with mock.patch.object(service, '_IDENTITY_CACHE') as cache:
            self.engine().end_reuse()
        cache.end_reuse.assert_called_once_with()

    def test_a_given_callable_replaces_it(self):
        given = mock.Mock()
        with mock.patch.object(service, '_IDENTITY_CACHE') as cache:
            self.engine(end_reuse=given).end_reuse()
        given.assert_called_once_with()
        cache.end_reuse.assert_not_called()
