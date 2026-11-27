"""Regression tests for v2 state base creation and permission narrowing."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from memvane.kernel import memvane_paths


class StateBasePermissionsTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix='memvane-state-base-')
        self.home = Path(self._temp.name).resolve()

    def tearDown(self):
        # Restore owner write bits before TemporaryDirectory cleanup.
        base = memvane_paths.state_base(self.home)
        if base.exists() and not base.is_symlink() and base.is_dir():
            try:
                base.chmod(0o700)
            except OSError:
                pass
        self._temp.cleanup()

    def test_ensure_state_dir_creates_state_base_and_subdirs_0700(self):
        target = memvane_paths.ensure_state_dir('auto-context', 'logs', home=self.home)
        base = memvane_paths.state_base(self.home)
        self.assertEqual(target, base / 'auto-context' / 'logs')
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((base / 'auto-context').lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o700)
        memvane_paths.ensure_state_layout(self.home)
        self.assertEqual((base / 'layout.json').read_bytes(), b'{"stateFormat":2}\n')

    def test_ensure_auto_context_dir_under_state_base_creates_0700_base(self):
        target = memvane_paths.state_base(self.home) / 'auto-context' / 'scan-state'
        result = memvane_paths.ensure_auto_context_dir(target, home=self.home)
        self.assertEqual(result, target)
        base = memvane_paths.state_base(self.home)
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o700)
        memvane_paths.ensure_state_layout(self.home)

    def test_ensure_state_layout_narrows_owned_0755_state_base_to_0700(self):
        base = memvane_paths.state_base(self.home)
        base.mkdir(parents=True, mode=0o755)
        base.chmod(0o755)
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o755)

        memvane_paths.ensure_state_layout(self.home)

        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o700)
        self.assertEqual((base / 'layout.json').read_bytes(), b'{"stateFormat":2}\n')

    def test_narrow_never_widens_owner_bits(self):
        base = memvane_paths.state_base(self.home)
        base.mkdir(parents=True, mode=0o755)
        (base / 'layout.json').write_bytes(b'{"stateFormat":2}\n')
        (base / 'layout.json').chmod(0o600)
        base.chmod(0o555)

        memvane_paths.ensure_state_layout(self.home)

        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o500)

    def test_ensure_state_layout_rejects_symlink_state_base_without_chmodding_target(self):
        real_target = self.home / 'real-target'
        real_target.mkdir(mode=0o755)
        real_target.chmod(0o755)
        base = memvane_paths.state_base(self.home)
        base.parent.mkdir(parents=True, mode=0o700)
        base.symlink_to(real_target, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, '^state_layout_root_invalid$'):
            memvane_paths.ensure_state_layout(self.home)
        self.assertEqual(stat.S_IMODE(real_target.lstat().st_mode), 0o755)

    def test_ensure_state_layout_rejects_symlink_ancestor_without_chmodding_target(self):
        real_parent = self.home / 'real-memvane'
        real_v2 = real_parent / 'v2'
        real_v2.mkdir(parents=True, mode=0o755)
        real_v2.chmod(0o755)
        base = memvane_paths.state_base(self.home)
        base.parent.parent.mkdir(parents=True, mode=0o700)
        base.parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, '^state_layout_root_invalid$'):
            memvane_paths.ensure_state_layout(self.home)
        self.assertEqual(stat.S_IMODE(real_v2.lstat().st_mode), 0o755)

    def test_ensure_state_layout_rejects_regular_file_state_base(self):
        base = memvane_paths.state_base(self.home)
        base.parent.mkdir(parents=True, mode=0o700)
        base.write_text('not a dir\n', encoding='utf-8')

        with self.assertRaisesRegex(ValueError, '^state_layout_root_invalid$'):
            memvane_paths.ensure_state_layout(self.home)

    def test_ensure_state_layout_rejects_foreign_owner_without_chmodding(self):
        base = memvane_paths.state_base(self.home)
        base.mkdir(parents=True, mode=0o755)
        base.chmod(0o755)
        foreign_uid = os.getuid() + 1

        with mock.patch.object(os, 'getuid', return_value=foreign_uid):
            with self.assertRaisesRegex(ValueError, '^state_layout_root_invalid$'):
                memvane_paths.ensure_state_layout(self.home)
        self.assertEqual(stat.S_IMODE(base.lstat().st_mode), 0o755)


if __name__ == '__main__':
    unittest.main()
