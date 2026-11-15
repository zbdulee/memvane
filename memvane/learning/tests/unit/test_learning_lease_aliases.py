"""Learning's lease and file helpers are the shared kernel/runtime primitives, not copies.

Checks where each helper is defined, without importing a runtime internal
(runtime keeps its own alias identity checks).
"""
import unittest

class Aliases(unittest.TestCase):
    def test_learning_modules_re_export_the_same_objects(self):
        from memvane.learning.context_learning import local_cycle, resident
        for value, name in ((resident._private_state, 'private_state'),
                            (resident._private_lock, 'private_lock'),
                            (local_cycle._atomic_json, 'atomic_json')):
            self.assertIn(value.__module__.split('.')[1], ('kernel', 'runtime'), value.__module__)
            self.assertEqual(value.__name__, name)
        self.assertEqual(local_cycle.MAX_FILE, 16 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
