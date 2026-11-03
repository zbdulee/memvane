#!/usr/bin/env python3
"""Actual managed base transaction admission and rollback/close ordering."""
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('write_windows',ROOT/'test/fixtures/laya-reader-windows-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

LOCK_CHILD=r'''
import fcntl,os,signal,sys
signal.alarm(20)
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
try:
    fcntl.flock(fd,(fcntl.LOCK_SH if sys.argv[2]=='SH' else fcntl.LOCK_EX)|fcntl.LOCK_NB)
except BlockingIOError:sys.exit(3)
print('ACQUIRED',flush=True)
if sys.argv[3]=='hold':
    if sys.stdin.readline()!='RELEASE\n':sys.exit(4)
os.close(fd)
'''


class WriteChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.ReaderWindowsChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case=self.fixture.seed,self.fixture.case
        self.W=importlib.import_module('laya_reader_windows')
    def lock(self,name):
        return self.W.R._lock_dir(self.seed.install,create=False)/name
    def probe(self,name,mode='EX'):
        result=subprocess.run([sys.executable,'-B','-I','-c',LOCK_CHILD,str(self.lock(name)),mode,'probe'],
            encoding='utf8',capture_output=True,timeout=4)
        if result.returncode not in (0,3):raise RuntimeError('lock probe failed: '+result.stderr)
        return result.returncode==0
    @contextmanager
    def held(self,name,mode):
        child=subprocess.Popen([sys.executable,'-B','-I','-c',LOCK_CHILD,str(self.lock(name)),mode,'hold'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],4)[0] or child.stdout.readline()!='ACQUIRED\n':
                raise RuntimeError('independent holder prerequisite failed')
            yield
        finally:
            try:out,err=child.communicate('RELEASE\n',timeout=4)
            except subprocess.TimeoutExpired:
                child.kill();child.communicate();raise
            if child.returncode!=0:raise RuntimeError('independent holder failed: '+err)
    @contextmanager
    def writer(self):
        from laya_index_a import LayaIndexA
        with self.seed.prepared():
            self.W.prepare_windows(self.seed.installer)
            component=self.W.component_for(self.seed.installer,'base')
            writer=LayaIndexA(self.case.db,coordination=component)
            try:yield writer,component
            finally:
                writer.close(deadline=time.monotonic()+1)
                self.W.drain_pending(deadline=time.monotonic()+1)
    def test_mutation_ex_blocks_content_before_begin_and_managed_autocheckpoint_is_off(self):
        with self.writer() as (writer,component):
            body='synthetic mutation admission content';digest=hashlib.sha256(body.encode()).hexdigest()
            auto=writer.db.execute('PRAGMA wal_autocheckpoint').fetchone()[0]
            sql=[];writer.db.set_trace_callback(sql.append)
            error=None
            with self.held('base-mutation.lock','EX'):
                try:writer.ensure_content(digest,body)
                except Exception as exc:error=type(exc).__name__+':'+str(exc)
                row=writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone()
            writer.db.set_trace_callback(None)
            begins=[s for s in sql if s.strip().upper().startswith('BEGIN')]
            print(json.dumps(dict(case='content-mutation-admission',error=error,begin=begins,
                inserted=row is not None,autoCheckpoint=auto)),flush=True)
            with self.subTest(boundary='mutation-before-sql-begin'):
                self.assertIsNotNone(error);self.assertEqual(begins,[]);self.assertIsNone(row)
            with self.subTest(boundary='managed-autocheckpoint-disabled'):
                self.assertEqual(auto,0)
    def test_cut_sh_blocks_exclusion_before_begin_but_private_content_still_commits(self):
        with self.writer() as (writer,component):
            scope=self.case.route['scopeId'];path='wiki/transaction-guard.md'
            body='synthetic content independent of serving cut';digest=hashlib.sha256(body.encode()).hexdigest()
            sql=[];error=None
            with self.held('publish-cut.lock','SH'):
                self.assertTrue(writer.ensure_content(digest,body))
                writer.db.set_trace_callback(sql.append)
                try:writer.exclude(scope,path=path)
                except Exception as exc:error=type(exc).__name__+':'+str(exc)
                rows=writer.db.execute('SELECT * FROM exclusions WHERE scope=? AND value=?',(scope,path)).fetchall()
            writer.db.set_trace_callback(None)
            begins=[s for s in sql if s.strip().upper().startswith('BEGIN')]
            print(json.dumps(dict(case='visible-exclusion-cut-admission',error=error,begin=begins,
                exclusions=rows,privateContentPresent=writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone()==(body,))),flush=True)
            self.assertIsNotNone(error);self.assertEqual(begins,[]);self.assertEqual(rows,[])
    def test_real_dml_fault_rolls_back_releases_logical_locks_then_native_close_takes_ex(self):
        with self.writer() as (writer,component):
            body='synthetic actual DML rollback sentinel';digest=hashlib.sha256(body.encode()).hexdigest()
            observations=[];closes=[];actual=type(writer.db).execute
            native=type(writer.db)._native_close_locked
            def execute(db,sql,*a,**kw):
                result=actual(db,sql,*a,**kw)
                if sql.startswith('INSERT INTO content_fts'):
                    observations.append(dict(transactionActive=db.in_transaction,
                        mutationExclusiveAvailable=self.probe('base-mutation.lock'),
                        cutExclusiveAvailable=self.probe('publish-cut.lock')))
                    raise RuntimeError('synthetic_after_real_content_dml')
                return result
            with mock.patch.object(type(writer.db),'execute',execute):
                with self.assertRaisesRegex(RuntimeError,'synthetic_after_real_content_dml'):
                    writer.ensure_content(digest,body)
            self.assertIsNone(writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone())
            after=dict(mutation=self.probe('base-mutation.lock'),cut=self.probe('publish-cut.lock'))
            def close(db):
                item=dict(transactionActive=db.in_transaction,mutation=self.probe('base-mutation.lock'),
                    cut=self.probe('publish-cut.lock'),physicalSharedAvailable=self.probe('base-physical.lock','SH'))
                native(db);closes.append(item)
            with mock.patch.object(type(writer.db),'_native_close_locked',close):
                writer.close(deadline=time.monotonic()+1)
            print(json.dumps(dict(case='real-dml-rollback-close-order',during=observations,after=after,nativeClose=closes)),flush=True)
            self.assertEqual(observations,[dict(transactionActive=True,mutationExclusiveAvailable=False,cutExclusiveAvailable=True)])
            self.assertEqual(after,dict(mutation=True,cut=True))
            self.assertEqual(closes,[dict(transactionActive=False,mutation=True,cut=True,physicalSharedAvailable=False)])


if __name__=='__main__':unittest.main(verbosity=2)
