import unittest
from memvane.kernel import cooldown


class CooldownGuards(unittest.TestCase):
    def test_expiry_rejects_values_outside_the_bounded_window(self):
        self.assertTrue(cooldown.expiry_active('120', 100))
        self.assertTrue(cooldown.expiry_active(100 + cooldown.MAX_COOLDOWN_SECS, 100))
        self.assertFalse(cooldown.expiry_active(101 + cooldown.MAX_COOLDOWN_SECS, 100))
        self.assertFalse(cooldown.expiry_active(99, 100))
        for value in ('1e300', 'inf', 'nan', 'bad', None):
            self.assertFalse(cooldown.expiry_active(value, 100), value)

    def test_future_mtime_is_elapsed_except_within_tolerance(self):
        self.assertTrue(cooldown.window_elapsed(200, 100, 60))
        self.assertFalse(cooldown.window_elapsed(150, 100, 60))
        self.assertTrue(cooldown.window_elapsed('bad', 100, 60))
        self.assertFalse(cooldown.window_elapsed(90, 100, 60))
        self.assertTrue(cooldown.window_elapsed(39, 100, 60))
        self.assertTrue(cooldown.window_elapsed(100, 100, 0))
        self.assertEqual(cooldown.LOCK_STALE_SECS, 600)

    def test_cooldown_write_length_is_clamped(self):
        self.assertEqual(cooldown.clamp_seconds(-2), 0)
        self.assertEqual(cooldown.clamp_seconds(10**9), cooldown.MAX_COOLDOWN_SECS)


if __name__ == '__main__':
    unittest.main()
