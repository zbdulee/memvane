"""Inactive candidate v2 schema and FTS contract; all data is synthetic."""
import contextlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
import uuid

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('blob_fixture',HERE/'laya-blob-store-checks.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)


class ViewSchemaChecks(helpers.BlobStoreCase):
    def setUp(self):
        super().setUp()
        try:
            self.fts=importlib.import_module('laya_view_fts')
            self.schema=importlib.import_module('laya_blob_schema')
            importlib.import_module('laya_view_schema')
        except ModuleNotFoundError as exc:
            if exc.name not in ('laya_view_fts','laya_view_schema'):raise
            self.fail('PR3 candidate view schema/FTS module is absent')

    def fts_db(self):
        db=sqlite3.connect(':memory:',cached_statements=0)
        self.addCleanup(db.close)
        db.execute('CREATE TABLE generations(ordinal INTEGER PRIMARY KEY,state TEXT NOT NULL)')
        db.execute("INSERT INTO generations VALUES(1,'building')")
        return db

    def test_01_v2_private_store_has_all_fixed_generation_tables(self):
        store,_,_=self.create()
        self.assertEqual((self.schema.FORMAT,self.schema.SCHEMA_VERSION),
                         ('memvane-git-blob-candidate-v2',2))
        data=self.db.read_bytes()
        self.assertEqual((int.from_bytes(data[68:72],'big'),int.from_bytes(data[60:64],'big')),
                         (0x4D564231,2))
        expected={'views','generations','wiki_members','source_members','card_sources',
                  'generation_seals','view_heads'}
        self.assertTrue(expected.issubset(store.counts()))
        self.assertTrue(store.verify())

    def test_02_v1_and_v6_headers_are_refused_without_writes(self):
        repository,_=self.bind()
        for version in [1,6]:
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

    def test_04_fts_names_are_integer_owned_and_contents_exact(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        rows=[dict(filepath='cards/a.md',title='Alpha',body='hello world',path_key='cards/a.md')]
        for value in [True,0,-1,'1','1;DROP TABLE generations',None]:
            with self.subTest(value=value):self.refused(self.fts.table,value)
        with guard.building(1):
            self.fts.create(db,1)
            self.fts.populate(db,1,rows)
        db.commit()
        self.fts.assert_rows(db,1,rows)
        proof=self.fts.proof(db,1,rows)
        self.assertIsInstance(proof,str);self.assertEqual(len(proof),64)
        db.commit();self.assertEqual(self.fts.proof(db,1,rows),proof)
        with self.assertRaises(self.Error):self.fts.assert_rows(db,1,[])
        with self.assertRaises(self.Error):self.fts.assert_rows(db,1,[dict(rows[0],body='changed')])
        self.assertEqual(db.execute("SELECT path_key FROM wiki_fts_1 WHERE wiki_fts_1 MATCH 'hello'").fetchall(),[('cards/a.md',)])

    def test_05_build_capability_scoped_and_sealed_writes_refused(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        rows=[dict(filepath='a.md',title='A',body='hello',path_key='a.md')]
        with guard.building(1):
            self.fts.create(db,1);self.fts.populate(db,1,rows)
            with self.assertRaises(self.Error):
                with guard.building(1):pass
        db.commit()
        before=self.fts.proof(db,1,rows)
        attempts=[("INSERT INTO wiki_fts_1 VALUES('b.md','B','hello','b.md')",()),
                  ('UPDATE wiki_fts_1_content SET c2=?',('changed',)),
                  ('DELETE FROM wiki_fts_1_data',()),
                  ("INSERT INTO wiki_fts_1(wiki_fts_1) VALUES('optimize')",()),
                  ('DROP TABLE wiki_fts_1',()),("ATTACH ':memory:' AS other",()),
                  ('PRAGMA writable_schema=ON',())]
        for sql,args in attempts:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.Error):db.execute(sql,args)
        self.assertEqual(self.fts.proof(db,1,rows),before)
        db.execute("UPDATE generations SET state='prepared'");db.commit()
        with self.assertRaises(self.Error):
            with guard.building(1):pass
        db.execute('PRAGMA query_only=ON')
        self.assertEqual(db.execute("SELECT path_key FROM wiki_fts_1 WHERE wiki_fts_1 MATCH 'hello'").fetchall(),[('a.md',)])

    def test_06_hidden_postings_tamper_changes_opaque_commitment(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        rows=[dict(filepath='a.md',title='A',body='hello world',path_key='a.md')]
        with guard.building(1):self.fts.create(db,1);self.fts.populate(db,1,rows)
        db.commit();before=self.fts.proof(db,1,rows)
        db.set_authorizer(None)  # unsupported separate SQL writer; integrity proof must notice
        record=db.execute('SELECT id,block FROM wiki_fts_1_data ORDER BY id DESC LIMIT 1').fetchone()
        raw=record[1];self.assertTrue(raw)
        changed=raw[:-1]+bytes([raw[-1]^1])
        db.execute('UPDATE wiki_fts_1_data SET block=? WHERE id=?',(changed,record[0]));db.commit()
        self.assertNotEqual(self.fts.proof(db,1,rows),before)
        # No MATCH against a deliberately corrupted inverted index.

    def test_07_dynamic_fts_schema_ownership_is_exact(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        with self.assertRaises(self.Error):self.fts.schema_rows(db)
        with guard.building(1):self.fts.create(db,1)
        db.commit()
        self.assertTrue(self.fts.schema_rows(db))
        db.set_authorizer(None)
        db.execute('CREATE TABLE wiki_fts_1_extra(value TEXT)')
        with self.assertRaises(self.Error):self.fts.schema_rows(db)


if __name__=='__main__':unittest.main()
