#!/usr/bin/env python3
"""Actual sidecar document publication admission, evidence and rollback."""
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sidecar_writer_owner',ROOT/'test/fixtures/laya-reader-sidecar-owner-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class SidecarWriteChecks(unittest.TestCase):
    def setUp(self):
        def seed_before_conversion(seed):
            with sqlite3.connect(seed.sidecar) as db:
                seed.sidecar_test_row=db.execute('SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 1').fetchone()
                self.assertIsNotNone(seed.sidecar_test_row)
                db.execute('DELETE FROM docs_fts WHERE path=?',(seed.sidecar_test_row[0],))
                db.execute('DELETE FROM docs WHERE path=?',(seed.sidecar_test_row[0],))
                if self._testMethodName == 'test_baseline_accepts_existing_empty_title_and_scan_id':
                    rowid,path,body=db.execute('SELECT rowid,path,body FROM docs ORDER BY path LIMIT 1').fetchone()
                    db.execute('DELETE FROM docs_fts WHERE rowid=?',(rowid,))
                    db.execute("UPDATE docs SET title='',scan_id='' WHERE rowid=?",(rowid,))
                    db.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',(rowid,path,'',body))
                if self._testMethodName == 'test_empty_sidecar_baseline_is_distinct_from_first_published_document':
                    db.execute('DELETE FROM docs_fts');db.execute('DELETE FROM docs')
        seed_class=existing.components_fixture.existing.seed_module.MoveSealChecks
        self.fixture=existing.SidecarOwnerChecks('runTest');self.addCleanup(self.fixture.doCleanups)
        with mock.patch.object(seed_class,'before_conversion',seed_before_conversion,create=True):
            self.fixture.setUp()
        self.seed,self.case,self.I,self.W=self.fixture.seed,self.fixture.case,self.fixture.incremental,self.fixture.windows
        self.identity=self.fixture.identity
    @contextmanager
    def opened(self,*,mixed=False):
        with self.seed.prepared():
            # A valid existing sidecar with two completed rows. The removed
            # third member has actual cached vectors and complete source proof.
            row=self.seed.sidecar_test_row
            component=self.fixture.component();self.fixture.tools.require_runtime_api()
            _windows,components=self.fixture.tools.components()
            if mixed:components.pop('base')
            runtime=self.fixture.tools.runtime(components)
            snapshot=runtime.catalog.require_complete()
            try:yield runtime,snapshot,row,component
            finally:runtime.close(deadline=time.monotonic()+1);self.W.drain_pending(deadline=time.monotonic()+1)
    @contextmanager
    def held(self,name,*,shared=False):
        parent=self.seed.reader._lock_dir(self.seed.install,create=False)
        fd=os.open(parent/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            fcntl.flock(fd,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            yield
        finally:os.close(fd)
    def rows(self):
        with self.seed.ro(self.fixture.sidecar) as db:
            return (db.execute('SELECT rowid,* FROM docs ORDER BY rowid').fetchall(),
                    db.execute('SELECT rowid,* FROM docs_fts ORDER BY rowid').fetchall())
    def publish(self,runtime,snapshot,row,component):
        return self.I.publish_document(runtime,self.identity,snapshot,*row,require_verified=False,coordination=component)
    def test_visible_cut_blocks_document_dml_and_keeps_both_tables(self):
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows();error=None;result=None
            with self.held('publish-cut.lock',shared=True):
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            after=self.rows()
            print(json.dumps(dict(case='sidecar-document-cut',error=error,result=result,rowsUnchanged=before==after)),flush=True)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==after)
    def test_mutation_ex_blocks_setup_and_document_write_before_begin(self):
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows();error=None;traces=[];opened=self.I._open
            def trace(*args,**kwargs):
                db=opened(*args,**kwargs);db.set_trace_callback(traces.append);return db
            with self.held('sidecar-mutation.lock'),mock.patch.object(self.I,'_open',trace):
                try:self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            begins=[sql for sql in traces if sql=='BEGIN IMMEDIATE']
            print(json.dumps(dict(case='sidecar-document-mutation',error=error,begin=begins,rowsUnchanged=before==self.rows())),flush=True)
            self.assertIsNotNone(error);self.assertEqual(begins,[]);self.assertTrue(before==self.rows())
    def test_actual_exclusion_after_preparation_prevents_sidecar_insert(self):
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows();setup=self.I._setup;injected=[];error=None;result=None
            def exclude_after_setup(*args,**kwargs):
                value=setup(*args,**kwargs)
                if getattr(args[0],'_window_owner',None) is not None and not injected:
                    runtime.index.exclude(runtime.scope,hash_=row[1]);injected.append(True)
                return value
            with mock.patch.object(self.I,'_setup',exclude_after_setup):
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-preparation-exclusion',injected=injected,error=error,result=result,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(injected,[True]);self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())
    def test_baseline_accepts_existing_empty_title_and_scan_id(self):
        with self.opened() as (runtime,snapshot,row,component):
            self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
            with self.seed.ro(self.fixture.sidecar) as db:
                baseline=json.loads(db.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()[0])
            self.assertEqual(baseline['rows'],2)

    def test_empty_sidecar_baseline_is_distinct_from_first_published_document(self):
        with self.opened() as (runtime,snapshot,row,component):
            self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
            with self.seed.ro(self.fixture.sidecar) as db:
                items=db.execute('SELECT revision,value FROM publication_revisions ORDER BY revision').fetchall()
            self.assertEqual([item[0] for item in items],[0,1])
            self.assertEqual(json.loads(items[0][1])['rows'],0)
            self.assertIsNone(json.loads(items[1][1])['before'])

    def test_baseline_refuses_fts_mismatch_without_publishing_or_healing(self):
        with self.opened() as (runtime,snapshot,row,component):
            with self.W.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                db=sqlite3.connect(self.fixture.sidecar.as_uri()+'?mode=rw',uri=True,isolation_level=None)
                try:
                    existing=db.execute('SELECT rowid FROM docs_fts ORDER BY rowid LIMIT 1').fetchone()
                    self.assertIsNotNone(existing)
                    db.execute('DELETE FROM docs_fts WHERE rowid=?',existing)
                finally:db.close()
            before=self.rows()
            with self.assertRaisesRegex(ValueError,'invalid_sidecar_baseline|reader_sidecar_revision_unproved'):
                self.publish(runtime,snapshot,row,component)
            self.assertEqual(self.rows(),before)
            with self.seed.ro(self.fixture.sidecar) as db:
                self.assertEqual(db.execute('SELECT * FROM publication_revisions').fetchall(),[])

    def test_baseline_rejects_corrupt_fts_index_with_content_rows_intact(self):
        with self.opened() as (runtime,snapshot,row,component):
            with self.W.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                db=sqlite3.connect(self.fixture.sidecar.as_uri()+'?mode=rw',uri=True,isolation_level=None)
                try:
                    db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main,docs_fts,'row')")
                    term=db.execute('SELECT term FROM temp.vocab ORDER BY term LIMIT 1').fetchone()[0]
                    self.assertGreater(db.execute('SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?',
                                                  ('"'+term.replace('"','""')+'"',)).fetchone()[0],0)
                    content=db.execute('SELECT path,title,body FROM docs_fts ORDER BY path').fetchall()
                    self.assertGreater(db.execute('SELECT count(*) FROM docs_fts_data WHERE id>10').fetchone()[0],0)
                    db.execute('DELETE FROM docs_fts_data WHERE id>10')
                    self.assertEqual(db.execute('SELECT path,title,body FROM docs_fts ORDER BY path').fetchall(),content)
                    try:matches=db.execute('SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?',
                                           ('"'+term.replace('"','""')+'"',)).fetchone()[0]
                    except sqlite3.DatabaseError:matches=0
                    self.assertEqual(matches,0,'fixture must actually break the search index')
                finally:db.close()
            before=self.rows()
            with self.assertRaisesRegex((ValueError,sqlite3.DatabaseError),'invalid_sidecar_baseline|reader_sidecar_revision_unproved|malformed'):
                self.publish(runtime,snapshot,row,component)
            self.assertEqual(self.rows(),before)
            with self.seed.ro(self.fixture.sidecar) as db:
                self.assertEqual(db.execute('SELECT * FROM publication_revisions').fetchall(),[])

    def test_partial_revision_schema_is_refused_without_healing_or_publication(self):
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows()
            with self.W.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                db=sqlite3.connect(self.fixture.sidecar.as_uri()+'?mode=rw',uri=True,isolation_level=None)
                try:db.execute('DROP TRIGGER publication_revision_delete_seal')
                finally:db.close()
            with self.assertRaisesRegex(Exception,'incremental_schema_changed'):
                self.publish(runtime,snapshot,row,component)
            self.assertEqual(self.rows(),before)
            with self.seed.ro(self.fixture.sidecar) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                            "AND name='publication_revision_delete_seal'").fetchall(),[])
                self.assertEqual(db.execute('SELECT count(*) FROM publication_revisions').fetchone(),(0,))

    def test_revision_insert_fault_rolls_back_document_and_replay_is_noop(self):
        import laya_publication_revision as revisions
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows()
            def receipts():
                with self.seed.ro(self.fixture.sidecar) as db:
                    return db.execute('SELECT revision,value FROM publication_revisions ORDER BY revision').fetchall()
            original=receipts();actual=revisions.record_sidecar
            def fail_after_insert(*args,**kwargs):
                actual(*args,**kwargs)
                raise RuntimeError('synthetic_sidecar_revision_failure')
            with mock.patch.object(revisions,'record_sidecar',side_effect=fail_after_insert):
                with self.assertRaisesRegex(RuntimeError,'synthetic_sidecar_revision_failure'):
                    self.publish(runtime,snapshot,row,component)
            self.assertEqual(self.rows(),before)
            baseline=receipts()
            self.assertEqual(len(baseline),len(original)+1)
            self.assertEqual(baseline[0][0],0)
            self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'published')
            committed=receipts();self.assertEqual(len(committed),len(baseline)+1)
            receipt=json.loads(committed[-1][1])
            self.assertEqual(receipt['component'],'sidecar');self.assertIsNone(receipt['before'])
            self.assertIsNotNone(receipt['after'])
            self.assertEqual(self.publish(runtime,snapshot,row,component)['status'],'already_published')
            self.assertEqual(receipts(),committed)
            with self.W.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                db=sqlite3.connect(self.fixture.sidecar.as_uri()+'?mode=rw',uri=True,isolation_level=None)
                try:
                    for sql in ('UPDATE publication_revisions SET value=? WHERE revision=1',
                                'INSERT OR REPLACE INTO publication_revisions(revision,value) VALUES(1,?)'):
                        with self.assertRaises(sqlite3.IntegrityError):db.execute(sql,('tampered',))
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute('DELETE FROM publication_revisions WHERE revision=1')
                finally:db.close()
            self.assertEqual(receipts(),committed)

    def test_actual_document_and_fts_insert_fault_rolls_back_together(self):
        with self.opened() as (runtime,snapshot,row,component):
            before=self.rows();execute=self.W._OwnedSQLite.execute;faults=[];error=None
            def fail(db,sql,*args,**kwargs):
                cursor=execute(db,sql,*args,**kwargs)
                if sql.startswith('INSERT INTO docs_fts'):
                    faults.append(True);raise RuntimeError('synthetic_after_actual_document_fts_insert')
                return cursor
            with mock.patch.object(self.W._OwnedSQLite,'execute',fail):
                try:self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-doc-fts-rollback',faults=faults,error=error,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(faults,[True]);self.assertIn('synthetic_after_actual_document_fts_insert',error or '')
            self.assertTrue(before==self.rows())
    def test_unmanaged_base_cannot_authorize_managed_sidecar_publication(self):
        with self.opened(mixed=True) as (runtime,snapshot,row,component):
            self.assertIsNone(runtime.index._connection_owner)
            before=self.rows();sql=[];runtime.index.db.set_trace_callback(sql.append);error=None;result=None
            try:
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            finally:runtime.index.db.set_trace_callback(None)
            print(json.dumps(dict(case='sidecar-unmanaged-base-refusal',error=error,result=result,baseSQL=len(sql),rowsUnchanged=before==self.rows())),flush=True)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertEqual(sql,[]);self.assertTrue(before==self.rows())

    def test_catalog_live_handle_swap_is_refused_before_any_source_sql(self):
        with self.opened() as (runtime,snapshot,row,component):
            original=runtime.catalog.db
            replacement=sqlite3.connect(runtime.catalog.path.as_uri()+'?mode=rw',uri=True,isolation_level=None)
            before=self.rows();sql=[];runtime.catalog.db=replacement
            replacement.set_trace_callback(sql.append);runtime.index.db.set_trace_callback(sql.append)
            error=None;result=None
            try:
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            finally:
                runtime.index.db.set_trace_callback(None);runtime.catalog.db=original;replacement.close()
            print(json.dumps(dict(case='sidecar-source-live-handle-swap',error=error,result=result,sourceSQL=len(sql),rowsUnchanged=before==self.rows())),flush=True)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertEqual(sql,[]);self.assertTrue(before==self.rows())

    def test_actual_trigger_added_after_setup_cannot_split_doc_and_fts(self):
        with self.opened() as (runtime,snapshot,row,component):
            setup=self.I._setup;before=self.rows();injected=[];error=None;result=None
            def changed(*args,**kwargs):
                value=setup(*args,**kwargs)
                if getattr(args[0],'_window_owner',None) is not None and not injected:
                    with self.W.physical_window(component,exclusive=True,deadline=time.monotonic()+1):
                        other=sqlite3.connect(self.fixture.sidecar.as_uri()+'?mode=rw',uri=True,isolation_level=None)
                        try:
                            other.execute('BEGIN IMMEDIATE')
                            other.execute('CREATE TRIGGER delete_inserted_doc AFTER INSERT ON docs '
                                          'BEGIN DELETE FROM docs WHERE rowid=NEW.rowid; END')
                            other.commit();injected.append(True)
                        finally:other.close()
                return value
            with mock.patch.object(self.I,'_setup',changed):
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-setup-schema-race',error=error,result=result,injected=injected,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(injected,[True]);self.assertIsNotNone(error);self.assertIsNone(result);self.assertTrue(before==self.rows())

    def test_explicitly_closed_retained_cursor_allows_valid_publication(self):
        with self.opened() as (runtime,snapshot,row,component):
            cursor=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash')
            cursor.fetchone();cursor.close();error=None;result=None
            try:result=self.publish(runtime,snapshot,row,component)
            except Exception as exc:error=str(exc)
            print(json.dumps(dict(case='sidecar-closed-source-cursor',error=error,result=result,retainedCursor=True)),flush=True)
            self.assertIsNotNone(cursor);self.assertIsNone(error);self.assertEqual(result['status'],'published')

    def test_unfinished_source_cursor_is_deferred_without_consuming_its_next_row(self):
        with self.opened() as (runtime,snapshot,row,component):
            expected=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash').fetchall()
            self.assertGreaterEqual(len(expected),2)
            cursor=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash');first=cursor.fetchone()
            error=None;result=None;before=self.rows()
            try:
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
                second=cursor.fetchone()
            finally:cursor.close()
            print(json.dumps(dict(case='sidecar-active-source-cursor',error=error,result=result,nextRowPreserved=second==expected[1],rowsUnchanged=before==self.rows())),flush=True)
            self.assertIsNotNone(error);self.assertIsNone(result);self.assertEqual((first,second),(expected[0],expected[1]))
            self.assertTrue(before==self.rows())

    def test_exhausted_retained_source_cursor_allows_valid_publication(self):
        with self.opened() as (runtime,snapshot,row,component):
            cursor=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash')
            self.assertTrue(cursor.fetchall());error=None;result=None
            try:
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            finally:cursor.close()
            print(json.dumps(dict(case='sidecar-exhausted-source-cursor',error=error,result=result)),flush=True)
            self.assertIsNone(error);self.assertEqual(result['status'],'published')

    def test_row_factory_sentinels_do_not_attest_native_cursor_exhaustion(self):
        with self.opened() as (runtime,snapshot,row,component):
            expected=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash').fetchall()
            self.assertGreaterEqual(len(expected),2);before=self.rows()
            for mode in ('none', 'stop'):
                cursor=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash')
                def sentinel(_cursor,_row):
                    if mode=='stop':raise StopIteration('synthetic row factory sentinel')
                    return None
                cursor.row_factory=sentinel;error=None;result=None
                try:
                    if mode=='none':self.assertIsNone(cursor.fetchone())
                    else:
                        with self.assertRaises(StopIteration):next(cursor)
                    try:result=self.publish(runtime,snapshot,row,component)
                    except Exception as exc:error=str(exc)
                    cursor.row_factory=None;remaining=cursor.fetchall()
                finally:cursor.close()
                print(json.dumps(dict(case='sidecar-row-factory-sentinel',mode=mode,error=error,result=result,
                    remainingRowsPreserved=remaining==expected[1:],rowsUnchanged=before==self.rows())),flush=True)
                with self.subTest(mode=mode):
                    self.assertIsNotNone(error);self.assertIsNone(result)
                    self.assertEqual(remaining,expected[1:]);self.assertTrue(before==self.rows())

    def test_idle_unexecuted_source_cursor_allows_valid_publication(self):
        with self.opened() as (runtime,snapshot,row,component):
            cursor=runtime.index.db.cursor();error=None;result=None
            try:
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
            finally:cursor.close()
            print(json.dumps(dict(case='sidecar-idle-source-cursor',error=error,result=result)),flush=True)
            self.assertIsNone(error);self.assertEqual(result['status'],'published')

    def test_factory_public_description_cannot_attest_native_completion(self):
        class ShadowDescription(sqlite3.Cursor):
            @property
            def description(self):return None
        self.shadow_factory_case(ShadowDescription,False)

    def test_factory_public_arraysize_cannot_attest_native_completion(self):
        class ShadowArraySize(sqlite3.Cursor):
            @property
            def arraysize(self):return 999
        self.shadow_factory_case(ShadowArraySize,True)

    def shadow_factory_case(self,factory,many):
        with self.opened() as (runtime,snapshot,row,component):
            expected=runtime.index.db.execute('SELECT hash FROM content ORDER BY hash').fetchall()
            self.assertGreaterEqual(len(expected),3);before=self.rows()
            cursor=runtime.index.db.cursor(factory=factory)
            cursor.execute('SELECT hash FROM content ORDER BY hash');error=None;result=None
            try:
                first=cursor.fetchmany() if many else [cursor.fetchone()]
                try:result=self.publish(runtime,snapshot,row,component)
                except Exception as exc:error=str(exc)
                remaining=cursor.fetchall()
            finally:cursor.close()
            print(json.dumps(dict(case='sidecar-native-cursor-descriptor',factory=factory.__name__,error=error,result=result,
                remainingRowsPreserved=first+remaining==expected,rowsUnchanged=before==self.rows())),flush=True)
            self.assertEqual(len(first),1);self.assertIsNotNone(error);self.assertIsNone(result)
            self.assertEqual(first+remaining,expected);self.assertTrue(before==self.rows())

if __name__=='__main__':unittest.main(verbosity=2)
