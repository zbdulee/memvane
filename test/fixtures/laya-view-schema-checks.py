"""Format 4 store schema contract; all data is synthetic."""
import contextlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock
import uuid

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('blob_fixture',HERE/'laya-blob-store-checks.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)


class ViewSchemaChecks(helpers.BlobStoreCase):
    def test_shared_body_fts_normalization_is_reused(self):
        values=importlib.import_module('laya_view_values')
        body='합성 카드 본문'
        with mock.patch.object(values.primitives, '_qmd_fts_text', wraps=values.primitives._qmd_fts_text) as normalized:
            rows=values.fts_rows({'one.md':body, 'two.md':body}, titles={'one.md':'one', 'two.md':'two'})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(call.args == (body,) for call in normalized.call_args_list), 1)
        with self.assertRaisesRegex(Exception, 'fts_deadline_exceeded'):
            values.fts_rows({'one.md':body},deadline=0)

    def test_01_v4_private_store_has_all_fixed_generation_tables(self):
        store,_,_=self.create()
        self.assertEqual((self.schema.FORMAT,self.schema.SCHEMA_VERSION),
                         ('memvane-git-blob-candidate-v4',4))
        data=self.db.read_bytes()
        self.assertEqual((int.from_bytes(data[68:72],'big'),int.from_bytes(data[60:64],'big')),
                         (0x4D564231,4))
        expected={'views','s_generations','s_cards','s_sources','s_card_sources','s_heads'}
        actual = {row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(expected.issubset(actual))
        self.assertTrue(store.verify())

    def test_02_unsupported_headers_are_refused_without_writes(self):
        repository,_=self.bind()
        for version in [1,2,3,6]:
            path=self.state/f'old-{version}.db'
            with sqlite3.connect(path) as db:
                db.execute('PRAGMA application_id=0x4d564231')
                db.execute('PRAGMA user_version='+str(version))
                db.execute('CREATE TABLE marker(value TEXT)')
                db.execute('INSERT INTO marker VALUES(?)',('existing synthetic data',))
            path.chmod(0o600);before=path.read_bytes()
            self.refused(self.reopen,repository,path=path)
            self.assertEqual(path.read_bytes(),before)

    def test_03_view_binding_unique_and_immutable(self):
        store,_,wid=self.create()
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA foreign_keys=ON')
            values=('a'*64,wid,'non-git')
            db.execute('INSERT INTO views(view_id,worktree_id,branch_key) VALUES(?,?,?)',values)
            for sql,args in [
                ('INSERT OR REPLACE INTO views(view_id,worktree_id,branch_key) VALUES(?,?,?)',values),
                ('INSERT INTO views(view_id,worktree_id,branch_key) VALUES(?,?,?)',('b'*64,wid,'non-git')),
                ('UPDATE views SET branch_key=? WHERE view_id=?',('refs/heads/other','a'*64)),
                ('DELETE FROM views WHERE view_id=?',('a'*64,)),
                ('INSERT INTO views(view_id,worktree_id,branch_key) VALUES(?,?,?)',('c'*64,'f'*32,'non-git'))]:
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.Error):db.execute(sql,args)
            db.rollback()
        self.assertTrue(store.verify())

if __name__=='__main__':unittest.main()
