#!/usr/bin/env python3
"""Actual sidecar document publication admission, evidence and rollback."""
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sidecar_writer_owner',ROOT/'test/fixtures/laya-reader-sidecar-owner-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class SidecarWriteChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.SidecarOwnerChecks('runTest');self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case,self.I,self.W=self.fixture.seed,self.fixture.case,self.fixture.incremental,self.fixture.windows
        self.identity=self.fixture.identity
    @contextmanager
    def opened(self,*,mixed=False):
        with self.seed.prepared():
            # A valid existing sidecar with two completed rows. The removed
            # third member has actual cached vectors and complete source proof.
            db=self.I._open(self.case.state)
            try:
                self.I._setup(db,self.identity)
                row=db.execute('SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 1').fetchone()
                self.assertIsNotNone(row)
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM docs_fts WHERE path=?',(row[0],))
                db.execute('DELETE FROM docs WHERE path=?',(row[0],));db.commit()
            finally:self.I._close(db)
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

if __name__=='__main__':unittest.main(verbosity=2)
