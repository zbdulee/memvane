"""Regression tests: wake service_root creates a 0700 state base compatible with ensure_state_layout."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from memvane.kernel import memvane_paths
from memvane.runtime import service_state
from memvane.runtime.types import ServiceFailure


class ServiceRootStateBaseTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix='memvane-wake-base-')
        self.home = Path(self._temp.name).resolve()

    def tearDown(self):
        self._temp.cleanup()

    def test_wake_service_root_first_then_ensure_state_layout_succeeds(self):
        wake = service_state.service_root(create=True, home=self.home)
        base = memvane_paths.state_base(self.home)

        self.assertEqual(wake, base / 'wake')
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(wake.lstat().st_mode), 0o700)

        memvane_paths.ensure_state_layout(self.home)
        self.assertEqual((base / 'layout.json').read_bytes(), b'{"stateFormat":2}\n')

    def test_wake_service_root_narrows_existing_0755_state_base(self):
        base = memvane_paths.state_base(self.home)
        base.mkdir(parents=True, mode=0o755)
        base.chmod(0o755)

        wake = service_state.service_root(create=True, home=self.home)

        self.assertEqual(wake, base / 'wake')
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o700)
        memvane_paths.ensure_state_layout(self.home)

    def test_wake_service_root_rejects_symlink_and_foreign_owner_state_base(self):
        real_target = self.home / 'real-v2'
        real_target.mkdir(mode=0o755)
        real_target.chmod(0o755)
        base = memvane_paths.state_base(self.home)
        base.parent.mkdir(parents=True, mode=0o700)
        base.symlink_to(real_target, target_is_directory=True)

        with self.assertRaises(ServiceFailure):
            service_state.service_root(create=True, home=self.home)
        self.assertEqual(stat.S_IMODE(real_target.lstat().st_mode), 0o755)

        base.unlink()
        base.mkdir(mode=0o755)
        base.chmod(0o755)
        with mock.patch.object(os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ServiceFailure):
                service_state.service_root(create=True, home=self.home)
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
