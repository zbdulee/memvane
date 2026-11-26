"""Unit tests for ``memvane_version.same_major`` against ``STATE_FORMAT``."""
from __future__ import annotations

import unittest
from unittest import mock

from memvane.kernel import memvane_paths, memvane_version


class MemvaneVersionTests(unittest.TestCase):
    def test_same_major_matches_state_format_regardless_of_plugin_major(self):
        with mock.patch.object(memvane_version, 'plugin_version', return_value='1.1.4'):
            self.assertTrue(memvane_version.same_major(memvane_paths.STATE_FORMAT))
            self.assertFalse(memvane_version.same_major(1))
            self.assertFalse(memvane_version.same_major(memvane_paths.STATE_FORMAT + 1))

    def test_same_major_rejects_invalid_state_format(self):
        for bad in (0, -1, True, '2', None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, '^invalid_state_format$'):
                    memvane_version.same_major(bad)
