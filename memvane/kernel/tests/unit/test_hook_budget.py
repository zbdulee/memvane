import os
import unittest
from unittest.mock import patch
from memvane.kernel import hook_budget


class HookBudgetTests(unittest.TestCase):
    def tearDown(self):
        hook_budget._deadline = None

    def test_seconds_accepts_bounds_and_defaults_on_invalid_values(self):
        for value, expected in [('2', 2.0), ('20', 20.0), ('1', 18.0),
                                ('21', 18.0), ('nan', 18.0), ('bad', 18.0)]:
            with self.subTest(value=value), patch.dict(os.environ, {'QMD_HOOK_TOTAL_SECONDS': value}):
                self.assertEqual(hook_budget._seconds(), expected)

    def test_inherited_deadline_is_not_reset(self):
        with patch.object(hook_budget.time, 'monotonic', return_value=100.0), \
             patch.dict(os.environ, {'QMD_HOOK_DEADLINE_MONO': '104'}):
            self.assertEqual(hook_budget.arm(watchdog=False, export=False), 104.0)
            self.assertEqual(hook_budget.remaining(), 4.0)


if __name__ == '__main__':
    unittest.main()
