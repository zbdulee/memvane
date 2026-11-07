"""Inactive candidate v3 schema and FTS contract; all data is synthetic."""
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
        with mock.patch.object(values.legacy, '_qmd_fts_text', wraps=values.legacy._qmd_fts_text) as normalized:
            rows=values.fts_rows({'one.md':body, 'two.md':body}, titles={'one.md':'one', 'two.md':'two'})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(call.args == (body,) for call in normalized.call_args_list), 1)
        with self.assertRaisesRegex(Exception, 'fts_deadline_exceeded'):
            values.fts_rows({'one.md':body},deadline=0)

    def test_fts_proof_validates_expected_entries_once(self):
        db=self.fts_db()
        self.fts.create(db,1)
        rows=[dict(filepath='wiki/a.md',title='A',body='synthetic',path_key='a.md')]
        self.fts.populate(db,1,rows)
        with mock.patch.object(self.fts, '_entries', wraps=self.fts._entries) as entries:
            self.fts.proof(db,1,rows)
            self.assertEqual(entries.call_count,1)

    def setUp(self):
        super().setUp()
        try:
            self.fts=importlib.import_module('laya_view_fts')
            self.schema=importlib.import_module('laya_blob_schema')
            importlib.import_module('laya_view_schema')
        except ModuleNotFoundError as exc:
            if exc.name not in ('laya_view_fts','laya_view_schema'):raise
            self.fail('PR3 candidate view schema/FTS module is absent')

    def fts_db(self, path=':memory:'):
        db=sqlite3.connect(path,cached_statements=0)
        self.addCleanup(db.close)
        db.execute("CREATE TABLE generation_slots(ordinal INTEGER PRIMARY KEY,stage_json TEXT NOT NULL DEFAULT '{}')")
        db.execute('INSERT INTO generation_slots(ordinal) VALUES(1)')
        db.execute('CREATE TABLE generations(ordinal INTEGER PRIMARY KEY,state TEXT NOT NULL)')
        db.execute("INSERT INTO generations VALUES(1,'building')")
        return db

    def test_sidecar_fts_is_separately_sealed_and_generation_scoped(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        db.execute('UPDATE generation_slots SET stage_json=?',
                   (json.dumps({'legacy':{'sidecar_corpus':[]}}),))
        base=[dict(filepath='wiki/a.md',title='A',body='base term',path_key='wiki/a.md')]
        side=[dict(filepath='wiki/b.md',title='B',body='side term 한글',path_key='wiki/b.md'),
              dict(filepath='wiki/a.md',title='A',body='other term',path_key='wiki/a.md')]
        with guard.building(1):
            self.fts.create(db,1);self.fts.populate(db,1,base)
            self.fts.create(db,1,sidecar=True);self.fts.populate(db,1,side,sidecar=True)
            self.fts.verify_index(db,1,sidecar=True)
        base_proof=self.fts.proof(db,1,base)
        side_proof,side_storage=self.fts.proof(db,1,side,sidecar=True,return_storage=True)
        paired_lexical,paired_storage=self.fts.proof_pair(db,1,base,side)
        with self.assertRaisesRegex(Exception,'fts_rows_mismatch'):
            self.fts.proof_pair(db,1,base,list(reversed(side)))
        self.assertEqual(len(paired_lexical),64);self.assertEqual(len(paired_storage),64)
        actual_storage=self.fts.storage_digest
        def damaged_storage(connection,ordinal,deadline=None,*,sidecar=False):
            return '0'*64 if sidecar else actual_storage(connection,ordinal,deadline,sidecar=sidecar)
        with mock.patch.object(self.fts,'storage_digest',side_effect=damaged_storage), \
             mock.patch.object(self.fts,'assert_rows',side_effect=AssertionError('unverified FTS read')):
            with self.assertRaisesRegex(Exception,'fts_storage_mismatch'):
                self.fts.proof_pair(db,1,base,side,expected_storage=paired_storage)
        self.assertNotEqual(base_proof,side_proof)
        self.assertTrue(self.fts.schema_rows(db))
        self.assertEqual(db.execute("SELECT path_key FROM wiki_fts_side_1 WHERE wiki_fts_side_1 MATCH 'side'").fetchall(),
                         [('wiki/b.md',)])
        db.execute("CREATE VIRTUAL TABLE side_reference USING fts5(path,title,body,tokenize='porter unicode61')")
        for item in side:
            db.execute('INSERT INTO side_reference(path,title,body) VALUES(?,?,?)',
                       (item['filepath'],item['title'],item['body']))
        original=db.execute("SELECT bm25(side_reference,1.5,4.0,1.0) FROM side_reference WHERE side_reference MATCH 'side'").fetchone()[0]
        candidate=db.execute("SELECT bm25(wiki_fts_side_1,1.5,4.0,1.0,0.0) FROM wiki_fts_side_1 WHERE wiki_fts_side_1 MATCH 'side'").fetchone()[0]
        self.assertAlmostEqual(original,candidate,places=12)
        original=db.execute("SELECT bm25(side_reference,1.5,4.0,1.0) FROM side_reference WHERE side_reference MATCH '한글'").fetchone()[0]
        candidate=db.execute("SELECT bm25(wiki_fts_side_1,1.5,4.0,1.0,0.0) FROM wiki_fts_side_1 WHERE wiki_fts_side_1 MATCH '한글'").fetchone()[0]
        self.assertAlmostEqual(original,candidate,places=12)
        with self.assertRaises(sqlite3.Error):
            db.execute("INSERT INTO wiki_fts_side_1 VALUES('c','c','c','c')")
        with self.assertRaisesRegex(Exception,'fts_storage_mismatch'):
            self.fts.proof(db,1,side,sidecar=True,expected_storage='0'*64)
        self.assertEqual(self.fts.storage_digest(db,1,sidecar=True),side_storage)
        self.assertEqual(self.fts.proof(db,1,base),base_proof)
        db.execute("UPDATE generation_slots SET stage_json='{}'")
        with self.assertRaises(self.Error):self.fts.schema_rows(db)
        db.execute("UPDATE generation_slots SET stage_json='{bad'")
        with self.assertRaises(self.Error):self.fts.schema_rows(db)

    def test_01_v3_private_store_has_all_fixed_generation_tables(self):
        store,_,_=self.create()
        self.assertEqual((self.schema.FORMAT,self.schema.SCHEMA_VERSION),
                         ('memvane-git-blob-candidate-v3',3))
        data=self.db.read_bytes()
        self.assertEqual((int.from_bytes(data[68:72],'big'),int.from_bytes(data[60:64],'big')),
                         (0x4D564231,3))
        expected={'views','generations','wiki_members','source_members','card_sources',
                  'generation_seals','view_heads'}
        self.assertTrue(expected.issubset(store.counts()))
        self.assertTrue(store.verify())

    def test_02_v1_v2_and_v6_headers_are_refused_without_writes(self):
        repository,_=self.bind()
        for version in [1,2,6]:
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
        path=self.state/'fts-tamper.db'
        db=self.fts_db(path);guard=self.fts.ConnectionGuard(db)
        rows=[dict(filepath='a.md',title='A',body='hello world',path_key='a.md')]
        with guard.building(1):self.fts.create(db,1);self.fts.populate(db,1,rows)
        db.commit();before=self.fts.proof(db,1,rows)
        helpers.corrupt_fts_block(path,1)
        self.assertNotEqual(self.fts.proof(db,1,rows),before)
        # No MATCH against a deliberately corrupted inverted index.

    def test_07_dynamic_fts_schema_ownership_is_exact(self):
        db=self.fts_db();guard=self.fts.ConnectionGuard(db)
        with self.assertRaises(self.Error):self.fts.schema_rows(db)
        with guard.building(1):self.fts.create(db,1)
        db.commit()
        self.assertTrue(self.fts.schema_rows(db))
        db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        db.execute('CREATE TABLE wiki_fts_1_extra(value TEXT)')
        with self.assertRaises(self.Error):self.fts.schema_rows(db)


    def test_08_fts_integrity_check_is_stable_and_detects_hidden_postings(self):
        path=self.state/'fts-integrity.db'
        db=sqlite3.connect(path,cached_statements=0);self.addCleanup(db.close)
        db.execute('CREATE TABLE generation_slots(ordinal INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO generation_slots VALUES(1)')
        db.execute('CREATE TABLE generations(ordinal INTEGER PRIMARY KEY,state TEXT NOT NULL)')
        db.execute("INSERT INTO generations VALUES(1,'building')")
        guard=self.fts.ConnectionGuard(db)
        rows=[dict(filepath='a.md',title='A',body='hello world',path_key='a.md')]
        with guard.building(1):self.fts.create(db,1);self.fts.populate(db,1,rows)
        before=self.fts.storage_digest(db,1)
        with guard.building(1):
            self.fts.verify_index(db,1)
            self.assertEqual(self.fts.storage_digest(db,1),before)
        self.assertEqual(self.fts.storage_digest(db,1),before)
        # A separate SQL writer is the actual inter-phase tamper boundary.
        # Direct same-connection shadow writes bypass FTS's private caches and
        # are never an API operation (opaque post-seal hashes still catch them).
        # Remove a referenced segment, rather than changing an allowed encoding detail.
        helpers.corrupt_fts_block(path,1,remove=True)
        with self.assertRaises(self.Error):
            with guard.building(1):self.fts.verify_index(db,1)


if __name__=='__main__':unittest.main()
