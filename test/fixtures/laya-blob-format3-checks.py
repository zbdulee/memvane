"""Actual frozen v2 structure and readonly format3 admission boundaries."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('blob_helpers', HERE / 'laya-blob-store-checks.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class FormatChecks(helpers.BlobStoreCase):
    def legacy_v2(self):
        """Real old DDL, with synthetic current fixture identity metadata."""
        from memvane.store import blob_schema as schema
        store, repository, _ = self.create(path=self.state / 'seed.db')
        meta = dict(store._db.execute('SELECT * FROM store_meta').fetchone())
        # The v2 worktree row also carried the settings path and byte digest.
        trees = [dict(row, settings_path=str(self.root / '.memvane/settings.json'), settings_sha='0' * 64)
                 for row in store._db.execute('SELECT * FROM worktrees')]
        store.close()
        frozen = json.loads((HERE / 'data/laya-blob-schema-v2.json').read_text('utf8'))
        self.assertEqual(frozen['source_commit'], '9f5421e3420c4f064ec21c0d95ffe909402e6537')
        self.assertEqual((frozen['version'], frozen['format'], len(frozen['statements'])),
                         (2, 'memvane-git-blob-candidate-v2', 97))
        path = self.state / 'legacy-v2.db'
        with sqlite3.connect(path) as db:
            for statement in frozen['statements']:
                db.execute(statement)
            db.execute('PRAGMA application_id=' + str(frozen['app_id']))
            db.execute('PRAGMA user_version=2')
            meta.update(format=frozen['format'], schema_version=2, schema_digest=schema.manifest(db))
            for table, rows in [('store_meta', [meta]), ('worktrees', trees)]:
                for row in rows:
                    db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES('
                               + ','.join('?' for _ in row) + ')', tuple(row.values()))
        path.chmod(0o600)
        # The RED run proves this is accepted by the actual untouched v2 code.
        # The final v3 run still requires refusal below, never legacy conversion.
        if schema.SCHEMA_VERSION == 2:
            old = self.track(self.Store.open(str(path), repository=repository, scope_id=self.scope))
            self.assertTrue(old.verify())
            old.close()
        return repository, path

    def readonly_refusal(self, repository, path):
        before = path.read_bytes()
        calls = []
        connect = sqlite3.connect

        def observe(target, *args, **kwargs):
            calls.append(str(target))
            return connect(target, *args, **kwargs)

        with mock.patch('sqlite3.connect', side_effect=observe):
            self.refused(self.reopen, repository, path=path)
        self.assertTrue(calls)
        self.assertTrue(all('mode=ro' in target for target in calls), calls)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + '-wal').exists())

    def test_new_file_uses_format3_and_exact_manifest(self):
        from memvane.store import blob_schema as schema
        store, _, _ = self.create()
        self.assertEqual((schema.FORMAT, schema.SCHEMA_VERSION),
                         ('memvane-git-blob-candidate-v3', 3))
        meta = store._db.execute('SELECT * FROM store_meta').fetchone()
        self.assertEqual((meta['format'], meta['schema_version']), (schema.FORMAT, 3))
        self.assertEqual(meta['schema_digest'], schema.expected_manifest())
        self.assertTrue(store.verify())

    def test_actual_v2_file_is_refused_readonly_without_conversion(self):
        repository, path = self.legacy_v2()
        self.readonly_refusal(repository, path)

    def test_other_headers_and_structural_tamper_are_readonly_refusals(self):
        repository, _ = self.bind()
        for version in (1, 6):
            path = self.state / ('header-' + str(version) + '.db')
            with sqlite3.connect(path) as db:
                db.execute('PRAGMA application_id=0x4d564231')
                db.execute('PRAGMA user_version=' + str(version))
                db.execute('CREATE TABLE existing_synthetic_marker(value TEXT)')
                db.execute("INSERT INTO existing_synthetic_marker VALUES('retained')")
            path.chmod(0o600)
            self.readonly_refusal(repository, path)
        store, repository, _ = self.create(path=self.state / 'tampered.db')
        store.close()
        path = self.state / 'tampered.db'
        with sqlite3.connect(path) as db:
            db.execute('DROP TRIGGER wiki_blobs_update')
        self.readonly_refusal(repository, path)

    def test_create_never_replaces_existing_actual_v2(self):
        repository, path = self.legacy_v2()
        before = path.read_bytes()
        _, worktree = self.bind()
        self.refused(self.Store.create, str(path), project_root=str(self.root),
                     repository=repository, worktree=worktree, scope_id=self.scope)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
