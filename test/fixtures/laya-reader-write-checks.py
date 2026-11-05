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
import sqlite3
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

    def test_expired_admission_never_begins_sql(self):
        with self.writer() as (writer,component):
            sql=[];entered=False;error=None
            writer.db.set_trace_callback(sql.append)
            try:
                with self.W.write_transaction(writer._connection_owner,visible=True,deadline=time.monotonic()-1):
                    entered=True
            except Exception as exc:error=type(exc).__name__+':'+str(exc)
            writer.db.set_trace_callback(None)
            begins=[s for s in sql if s.strip().upper().startswith('BEGIN')]
            print(json.dumps(dict(case='expired-write-admission',entered=entered,error=error,begin=begins)),flush=True)
            self.assertIsNotNone(error);self.assertFalse(entered);self.assertEqual(begins,[])
            self.assertTrue(self.probe('base-mutation.lock'));self.assertTrue(self.probe('publish-cut.lock'))

    def test_rollback_failure_retains_sql_and_both_locks_until_same_thread_drain(self):
        with self.writer() as (writer,component):
            scope=self.case.route['scopeId'];path='wiki/rollback-pending.md';attempts=[];events=[]
            cls=type(writer.db);execute=cls.execute;rollback=cls.rollback;native=cls._native_close_locked
            def actual_dml_fault(db,sql,*a,**kw):
                cursor=execute(db,sql,*a,**kw)
                if sql.startswith('INSERT OR IGNORE INTO exclusions'):
                    raise RuntimeError('synthetic_after_actual_visible_dml')
                return cursor
            def fail_once(db):
                attempts.append(db.in_transaction)
                if len(attempts)==1:raise RuntimeError('synthetic_first_rollback_failure')
                return rollback(db)
            def close(db):
                events.append(dict(transaction=db.in_transaction,
                    rows=db.execute('SELECT * FROM exclusions WHERE scope=? AND value=?',(scope,path)).fetchall(),
                    mutation=self.probe('base-mutation.lock'),cut=self.probe('publish-cut.lock'),
                    physicalSharedAvailable=self.probe('base-physical.lock','SH')))
                return native(db)
            with mock.patch.object(cls,'execute',actual_dml_fault),mock.patch.object(cls,'rollback',fail_once),mock.patch.object(cls,'_native_close_locked',close):
                with self.assertRaisesRegex(RuntimeError,'synthetic_after_actual_visible_dml'):
                    writer.exclude(scope,path=path)
                pending=self.W.pending_count();live=writer.db.in_transaction
                retained=dict(mutation=not self.probe('base-mutation.lock'),cut=not self.probe('publish-cut.lock'))
                with self.assertRaisesRegex(ValueError,'reader_lifetime_busy'):
                    with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
                remaining=self.W.drain_pending(deadline=time.monotonic()+1)
            print(json.dumps(dict(case='rollback-pending-same-thread-drain',pending=pending,
                actualTransactionLive=live,retained=retained,rollbackAttempts=attempts,nativeClose=events,
                remaining=remaining)),flush=True)
            self.assertEqual(pending,1);self.assertTrue(live)
            self.assertEqual(retained,dict(mutation=True,cut=True));self.assertEqual(attempts,[True,True])
            self.assertEqual(remaining,0)
            self.assertEqual(events,[dict(transaction=False,rows=[],mutation=True,cut=True,physicalSharedAvailable=False)])

    def test_failed_rollback_blocks_explicit_commit(self):
        self.rollback_only_case(False)

    def test_caught_rollback_failure_blocks_automatic_commit(self):
        self.rollback_only_case(True)

    def rollback_only_case(self,automatic):
        with self.writer() as (writer,component):
            scope=self.case.route['scopeId'];path='wiki/rollback-only.md';attempts=[];rows_at_close=[]
            cls=type(writer.db);rollback=cls.rollback;native=cls._native_close_locked
            def fail_once(db):
                attempts.append(db.in_transaction)
                if len(attempts)==1:raise RuntimeError('synthetic_rollback_only_failure')
                return rollback(db)
            def close(db):
                rows_at_close.extend(db.execute('SELECT * FROM exclusions WHERE scope=? AND value=?',(scope,path)).fetchall())
                return native(db)
            error=None;tx=None
            with mock.patch.object(cls,'rollback',fail_once),mock.patch.object(cls,'_native_close_locked',close):
                try:
                    with self.W.write_transaction(writer._connection_owner,visible=True,deadline=time.monotonic()+1) as tx:
                        writer.db.execute('INSERT INTO exclusions VALUES(?,?,?)',(scope,'path',path))
                        if automatic:
                            try:tx.rollback()
                            except RuntimeError:pass
                        else:
                            raise RuntimeError('original_body_failure')
                except Exception as exc:
                    if automatic:error=type(exc).__name__+':'+str(exc)
                if not automatic:
                    try:tx.commit()
                    except Exception as exc:error=type(exc).__name__+':'+str(exc)
                live=writer.db.in_transaction
                retained=dict(mutation=not self.probe('base-mutation.lock'),cut=not self.probe('publish-cut.lock'))
                self.W.drain_pending(deadline=time.monotonic()+1)
            print(json.dumps(dict(case='rollback-only-commit-refusal',automatic=automatic,error=error,
                actualTransactionRetained=live,locksRetained=retained,rowsAtNativeClose=rows_at_close,
                rollbackAttempts=attempts)),flush=True)
            self.assertIsNotNone(error);self.assertTrue(live)
            self.assertEqual(retained,dict(mutation=True,cut=True));self.assertEqual(rows_at_close,[])
            self.assertEqual(attempts,[True,True]);self.assertEqual(self.W.pending_count(),0)

    def test_sqlite_busy_begin_cannot_outlive_budget_and_enter_dml(self):
        code=r'''
import os,signal,sqlite3,sys,time
signal.alarm(5)
db=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=2)
db.execute('BEGIN IMMEDIATE');print('SQL_LOCKED',flush=True)
time.sleep(.6);db.rollback();db.close()
'''
        with self.writer() as (writer,component):
            scope=self.case.route['scopeId'];path='wiki/native-busy-deadline.md'
            child=subprocess.Popen([sys.executable,'-B','-I','-c',code,str(self.case.db)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
            entered=False;error=None;elapsed=None
            try:
                if not select.select([child.stdout],[],[],4)[0] or child.stdout.readline()!='SQL_LOCKED\n':
                    raise RuntimeError('actual independent SQLite writer prerequisite failed')
                start=time.monotonic()
                try:
                    with self.W.write_transaction(writer._connection_owner,visible=True,deadline=start+.03):
                        entered=True
                        writer.db.execute('INSERT INTO exclusions VALUES(?,?,?)',(scope,'path',path))
                except Exception as exc:error=type(exc).__name__+':'+str(exc)
                elapsed=time.monotonic()-start
            finally:
                try:out,err=child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill();child.communicate();raise
                if child.returncode!=0:raise RuntimeError('actual independent SQLite writer failed: '+err)
            rows=writer.db.execute('SELECT * FROM exclusions WHERE scope=? AND value=?',(scope,path)).fetchall()
            timeout=writer.db.execute('PRAGMA busy_timeout').fetchone()[0]
            print(json.dumps(dict(case='native-sqlite-busy-admission',error=error,entered=entered,
                seconds=elapsed,rows=rows,busyTimeoutAfter=timeout)),flush=True)
            self.assertIsNotNone(error);self.assertFalse(entered);self.assertEqual(rows,[])
            self.assertLess(elapsed,.4);self.assertEqual(timeout,2000)
            self.assertTrue(self.probe('base-mutation.lock'));self.assertTrue(self.probe('publish-cut.lock'))


if __name__=='__main__':unittest.main(verbosity=2)
