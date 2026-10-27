"""Installer-side capacity store creation (temp dirs only; no model or host)."""
from __future__ import annotations

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
        with self.assertRaisesRegex(install.InstallBlocked, 'capacity_store_mismatch'):
            self.prepare(other)

    def test_catalog_interrupted_before_its_schema_is_recreated(self):
        catalog = Path(str(self.db) + '.catalog.sqlite')
        catalog.write_bytes(b'')  # created, crashed before the schema commit
        catalog.chmod(0o600)
        paths = self.prepare()
        self.assertEqual(capacity_store_paths(self.db), paths)

    def test_store_for_a_rebuilt_db_fails_with_a_specific_reason(self):
        self.prepare()
        with self.assertRaisesRegex(install.InstallBlocked, 'capacity_store_mismatch'):
            install.prepare_capacity_stores(self.db, self.roots, SCOPE, '0' * 32)


if __name__ == '__main__':
    unittest.main()
