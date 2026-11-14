"""The inference service root is the runtime's root, including under a test runtime root."""
from memvane.learning.context_learning import inference_service
from memvane.runtime import api as runtime
from tests.support.case import FixtureCase


class ServiceRoot(FixtureCase):
    def test_service_queue_and_coordinator_resolve_one_root_under_the_test_runtime_root(self):
        runtime.service_root(create=True)
        root = inference_service.service_root()
        self.assertEqual(root, self.runtime / 'laya-inference')
        self.assertEqual(runtime.wake_queue().root, root)
        self.assertEqual(runtime.coordinator().root, root)


if __name__ == '__main__':
    import unittest
    unittest.main()
