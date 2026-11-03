#!/usr/bin/env python3
"""Warm proof-reader ownership and explicit cold rejection, real SQLite only."""
from contextlib import ExitStack, contextmanager
import gc
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock
import weakref

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('proof_reader_window_fixture',ROOT/'test/fixtures/laya-writer-proof-windows-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

def emit(label,**values):print(json.dumps(dict(case=label,**values),sort_keys=True),flush=True)

def files(path):
    result={}
    for suffix in ('','-wal','-shm','-journal'):
        p=Path(str(path)+suffix)
        if not os.path.lexists(p):result[suffix]=None;continue
        node=p.lstat()
        result[suffix]=(None if suffix=='-shm' else p.read_bytes(),node.st_dev,node.st_ino,
                        node.st_mode,node.st_uid,node.st_nlink)
    return result

class WriterProofReaderChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.WriterProofWindowsChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case,self.W=self.fixture.seed,self.fixture.case,self.fixture.W
        self.P=self.fixture.plan_module;self.sync=self.fixture.sync

    def api(self):
        if importlib.util.find_spec('laya_writer_proof_reader') is None:
            emit('proof-reader-feature-absence',actualV5=True,actualProofWindow=True)
            self.fail('proof reader feature RED after actual v5/plan/SyncLease/window baseline')
        module=importlib.import_module('laya_writer_proof_reader')
        for name in ('proof_reader','pending_proof_readers','drain_proof_readers'):
            self.assertTrue(callable(getattr(module,name,None)),name)
        return module

    @contextmanager
    def warm(self):
        from laya_index_a import LayaIndexA
        from laya_index_claim_queue import ClaimQueue
        base=LayaIndexA(self.case.db);queue=None
        try:
            queue=ClaimQueue.open(Path(self.fixture.plan.queue_path))
            self.assertEqual(base.exclude(self.case.route['scopeId'],path='proof-reader-sentinel.md'),'excluded')
            for path in (self.case.db,Path(self.fixture.plan.queue_path)):
                self.assertTrue(Path(str(path)+'-wal').exists(),'warm WAL prerequisite')
                self.assertTrue(Path(str(path)+'-shm').exists(),'warm SHM prerequisite')
            yield
        finally:
            if queue is not None:queue.close()
            base.close()

    def scope(self):
        stack=ExitStack()
        try:
            owner=stack.enter_context(self.sync.sync_owner(Path(self.fixture.plan.queue_path)))
            self.assertIsNotNone(owner)
            window=stack.enter_context(self.P.writer_proof_windows(self.fixture.plan,queue_owner=owner,
                deadline=time.monotonic()+20))
            self.P.validate_writer_proof_window(window)
            return stack,window
        except BaseException:stack.close();raise

    def test_first_pragma_and_native_close_failure_keep_owner_windows_and_pin_until_creator_drain(self):
        with self.warm():
            stack,window=self.scope();module=None;refs=[];events=[];blocked=[True]
            execute=self.W._OwnedSQLite.execute;native=self.W._OwnedSQLite._native_close_locked
            def observed(db,sql,*args,**kwargs):
                if sql.strip().replace(' ','').upper()=='PRAGMAQUERY_ONLY=ON':
                    refs.append(weakref.ref(db))
                    raise sqlite3.OperationalError('synthetic_first_query_only_failure')
                return execute(db,sql,*args,**kwargs)
            def close(db):
                events.append(dict(event='native-close-attempt',physicalEX=not self.fixture.probe('base-physical.lock','SH')))
                if blocked[0]:raise sqlite3.OperationalError('synthetic_native_close_failure')
                result=native(db);events.append(dict(event='native-close-completed'));return result
            with mock.patch.object(self.W._OwnedSQLite,'execute',new=observed),mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                try:
                    module=self.api()
                    with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):
                        with module.proof_reader(window,'base'):pass
                    self.assertTrue(refs,'actual managed first PRAGMA observer must run')
                    try:stack.close()
                    except (ValueError,RuntimeError,sqlite3.Error):pass
                    gc.collect()
                    self.assertIsNotNone(refs[0]());_active=refs[0]().in_transaction
                    pending=module.pending_proof_readers();self.assertGreaterEqual(pending,1)
                    self.assertFalse(self.fixture.probe('base-physical.lock','SH'))
                    self.assertFalse(self.fixture.probe('reader-lifetime.lock','EX'))
                    with self.sync.sync_owner(Path(self.fixture.plan.queue_path)) as other:self.assertIsNone(other)
                    other_results=[]
                    def other_thread():
                        try:other_results.append(module.drain_proof_readers(deadline=time.monotonic()+.03))
                        except (ValueError,RuntimeError) as exc:other_results.append(str(exc))
                    worker=threading.Thread(target=other_thread);worker.start();worker.join(2)
                    if worker.is_alive():raise RuntimeError('noncreator drain thread did not terminate')
                    self.assertEqual(module.pending_proof_readers(),pending)
                    blocked[0]=False
                    self.assertEqual(module.drain_proof_readers(deadline=time.monotonic()+1),0)
                    if refs[0]() is not None:
                        with self.assertRaises(sqlite3.ProgrammingError):_closed=refs[0]().in_transaction
                    count=len(events)
                    self.assertEqual(module.drain_proof_readers(deadline=time.monotonic()+1),0)
                    self.assertEqual(len(events),count)
                    self.assertTrue(self.fixture.probe('base-physical.lock','SH'))
                    self.assertTrue(self.fixture.probe('reader-lifetime.lock','EX'))
                    with self.sync.sync_owner(Path(self.fixture.plan.queue_path)) as other:self.assertIsNotNone(other)
                    emit('failed-proof-reader-owner',pendingBeforeDrain=pending,otherThread=other_results,events=events)
                finally:
                    blocked[0]=False
                    try:stack.close()
                    except (ValueError,RuntimeError,sqlite3.Error):pass
                    if module is not None:module.drain_proof_readers(deadline=time.monotonic()+2)

    def test_facade_reads_committed_wal_without_raw_authority_and_parent_exit_finalizes_cursor(self):
        with self.warm():
            stack,window=self.scope();refs=[];events=[]
            execute=self.W._OwnedSQLite.execute;native=self.W._OwnedSQLite._native_close_locked
            def observed(db,sql,*args,**kwargs):
                if not refs:refs.append(weakref.ref(db))
                return execute(db,sql,*args,**kwargs)
            def close(db):
                events.append(not self.fixture.probe('base-physical.lock','SH'))
                return native(db)
            try:
                module=self.api()
                with mock.patch.object(self.W._OwnedSQLite,'execute',new=observed),mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    context=module.proof_reader(window,'base');reader=context.__enter__()
                    cursor=reader.execute('SELECT value FROM exclusions WHERE value=?',('proof-reader-sentinel.md',))
                    self.assertEqual(cursor.fetchone(),('proof-reader-sentinel.md',))
                    cursor=reader.execute('SELECT value FROM exclusions ORDER BY value')
                    for obj,names in ((reader,('connection','backup','deserialize')),(cursor,('connection',))):
                        for name in names:self.assertFalse(hasattr(obj,name),name)
                    attach=self.case.home/'proof-reader-forbidden-attach.sqlite'
                    for sql in ("DELETE FROM exclusions",'PRAGMA query_only=OFF',"ATTACH DATABASE '"+str(attach)+"' AS forbidden"):
                        with self.subTest(sql=sql):
                            with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):reader.execute(sql)
                    self.assertFalse(attach.exists())
                    del context;gc.collect()
                    self.assertIsNotNone(refs[0]());_live=refs[0]().in_transaction
                    self.assertEqual(events,[],'discarding reader context must not finalize native handle')
                    stack.close()
                    self.assertTrue(events);self.assertTrue(all(events))
                    if refs[0]() is not None:
                        with self.assertRaises(sqlite3.ProgrammingError):_closed=refs[0]().in_transaction
                    with self.assertRaises(sqlite3.ProgrammingError):cursor.fetchone()
                    rollback_error=None
                    try:reader.rollback()
                    except Exception as exc:rollback_error=exc
                    self.assertIsInstance(rollback_error,sqlite3.ProgrammingError)
                    self.assertEqual(module.pending_proof_readers(),0)
                    emit('proof-reader-facade',walSentinelRead=True,nativeCloseUnderEX=events,rawConnectionExposed=False)
            finally:stack.close()

    def test_creator_drain_closes_live_reader_after_lexical_owner_expiry(self):
        with self.warm():
            module=self.api();owner_scope=ExitStack();context=None;refs=[]
            claim=owner_scope.enter_context(self.sync.sync_owner(Path(self.fixture.plan.queue_path)))
            context=self.P.writer_proof_windows(self.fixture.plan,queue_owner=claim,deadline=time.monotonic()+20)
            window=context.__enter__()
            execute=self.W._OwnedSQLite.execute
            def observe(db,*args,**kwargs):
                if not refs:refs.append(weakref.ref(db))
                return execute(db,*args,**kwargs)
            try:
                with mock.patch.object(self.W._OwnedSQLite,'execute',new=observe):
                    reader_context=module.proof_reader(window,'base');reader=reader_context.__enter__()
                    cursor=reader.execute('SELECT value FROM exclusions')
                raw=refs[0]()
                owner_scope.close()
                with self.assertRaises((ValueError,RuntimeError)):cursor.fetchone()
                before=module.pending_proof_readers()
                remaining=module.drain_proof_readers(deadline=time.monotonic()+1)
                physical_free=self.fixture.probe('base-physical.lock','SH')
                emit('expired-live-proof-drain',pendingBefore=before,remaining=remaining,physicalFree=physical_free)
                self.assertTrue(physical_free,'creator drain must unwind expired live proof, not report a false zero')
                self.assertEqual(remaining,0)
                with self.assertRaises(sqlite3.ProgrammingError):_closed=raw.in_transaction
                with self.sync.sync_owner(Path(self.fixture.plan.queue_path)) as fresh:self.assertIsNotNone(fresh)
            finally:
                owner_scope.close()
                if context is not None:
                    try:context.__exit__(None,None,None)
                    except (ValueError,RuntimeError,sqlite3.Error):pass
                module.drain_proof_readers(deadline=time.monotonic()+2)

    def test_cold_no_aux_base_rejects_before_first_sqlite_connect_without_file_changes(self):
        # Observe the ordinary last-close boundary, then make the proof reader
        # the first content opener. This is a refusal test, not cold success.
        with self.warm():pass
        self.assertFalse(Path(str(self.case.db)+'-wal').exists(),'cold base WAL prerequisite')
        self.assertFalse(Path(str(self.case.db)+'-shm').exists(),'cold base SHM prerequisite')
        stack,window=self.scope()
        try:
            module=self.api();before=files(self.case.db);error=None
            with self.fixture.fixture.traced() as calls:
                try:
                    with module.proof_reader(window,'base'):self.fail('cold reader unexpectedly returned')
                except (ValueError,RuntimeError,OSError) as exc:error=str(exc)
            emit('cold-proof-reader-rejected',reason=error,contentConnects=calls,filesUnchanged=files(self.case.db)==before)
            self.assertIsNotNone(error);self.assertIn('cold',error.lower())
            self.assertEqual(calls,[])
            self.assertTrue(files(self.case.db)==before,'cold refusal must not change main/aux names or nodes')
        finally:stack.close()

if __name__=='__main__':unittest.main()
