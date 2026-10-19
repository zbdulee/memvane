"""Installer-side capacity store creation (temp dirs only; no model or host)."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_product_install as install
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_runtime import capacity_store_paths

SCOPE = 'f' * 32


class CapacityInstallChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-capacity-install-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.roots = {'raw': str(self.base / 'raw'), 'wiki': str(self.base / 'wiki')}
        for root in self.roots.values():
            Path(root).mkdir()
        self.db = self.base / 'candidate.sqlite3'
        index = LayaIndexA(self.db, create=True)
        self.incarnation = index.incarnation()
        index.close()

    def prepare(self, roots=None):
        return install.prepare_capacity_stores(self.db, roots or self.roots, SCOPE,
                                               self.incarnation)

    def test_creates_private_stores_that_the_runtime_gate_finds(self):
        self.assertIsNone(capacity_store_paths(self.db))
        paths = self.prepare()
        self.assertEqual(capacity_store_paths(self.db), paths)
        for path in paths:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_rerun_is_idempotent_and_completes_an_interrupted_run(self):
        first = self.prepare()
        self.assertEqual(self.prepare(), first)
        os.unlink(first[1])  # interrupted after the catalog
        with self.assertRaisesRegex(IndexContractError, 'capacity_store_incomplete'):
            capacity_store_paths(self.db)
        self.assertEqual(self.prepare(), first)

    def test_existing_store_for_other_roots_is_refused(self):
        self.prepare()
        other = dict(self.roots, wiki=str(self.base / 'raw'))
        with self.assertRaises((IndexContractError, ValueError)):
            self.prepare(other)

    def test_origin_installer_keeps_capacity_stores_off_by_default(self):
        default = inspect.signature(install.prepare_owned_qmd_origin).parameters['capacity_stores']
        self.assertIs(default.default, False)


if __name__ == '__main__':
    unittest.main()
