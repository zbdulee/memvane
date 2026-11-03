#!/usr/bin/env python3
"""Explicit managed-owner WAL checkpoint contracts; no automatic activation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('checkpoint_real_write_fixture',ROOT/'test/fixtures/laya-reader-write-checks.py')
write_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(write_fixture)

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)
def image(path):
    result={}
    for suffix in ('','-wal','-shm','-journal'):
        file=Path(str(path)+suffix)
        result[suffix]=None if not os.path.lexists(file) else (file.read_bytes(),
            tuple(getattr(file.lstat(),key) for key in ('st_dev','st_ino','st_mode','st_uid','st_nlink')))
    return result
def fixed(sql):return sql.strip().upper().replace(' ','')=='PRAGMAMAIN.WAL_CHECKPOINT(TRUNCATE)'

class CheckpointChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=write_fixture.WriteChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.case,self.W=self.fixture.case,self.fixture.W
    def commit_wal(self,writer,label):
        before=self.case.db.read_bytes();body='synthetic explicit checkpoint WAL '+label
        digest=hashlib.sha256(body.encode('utf8')).hexdigest();writer.ensure_content(digest,body)
        wal=Path(str(self.case.db)+'-wal')
        if self.case.db.read_bytes()!=before or not wal.is_file() or wal.stat().st_size==0:
            raise RuntimeError('actual committed uncheckpointed WAL prerequisite missing')
        if writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone()!=(body,):
            raise RuntimeError('actual committed WAL content prerequisite missing')
        return digest,body,before
    def api(self):
        checkpoint=getattr(self.W,'checkpoint_owned',None)
        if not callable(checkpoint):
            emit('checkpoint-feature-absence',actualManagedV5Writer=True,actualCommittedWAL=True,
                 missing='checkpoint_owned',classification='new-T3-fixed-checkpoint-feature')
            self.fail('T3 feature RED after actual managed writer/WAL: checkpoint_owned missing')
        return checkpoint
    def test_fixed_checkpoint_truncates_real_wal_and_holds_mutation_and_physical_ex(self):
        with self.fixture.writer() as (writer,_component):
            digest,body,_before_commit=self.commit_wal(writer,'reset-success')
            checkpoint=self.api();before=image(self.case.db);observations=[]
            cls=type(writer.db);actual=cls.execute
            def execute(db,sql,*args,**kwargs):
                if db is writer.db and fixed(sql):
                    observations.append(dict(sql=sql,mutationSHAvailable=self.fixture.probe('base-mutation.lock','SH'),
                                             physicalSHAvailable=self.fixture.probe('base-physical.lock','SH')))
                return actual(db,sql,*args,**kwargs)
            with mock.patch.object(cls,'execute',new=execute):
                result=checkpoint(writer._connection_owner,deadline=time.monotonic()+1)
            after=image(self.case.db);wal=Path(str(self.case.db)+'-wal')
            row=writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone()
            emit('actual-fixed-wal-reset',result=result,sqlLockObservations=observations,
                mainChanged=after[''][0]!=before[''][0],walSize=None if not wal.exists() else wal.stat().st_size,
                actualRowReadable=row==(body,),pending=self.W.pending_count())
            self.assertEqual(result,dict(state='reset_complete',checkpoint=(0,0,0)))
            self.assertEqual(len(observations),1);self.assertTrue(fixed(observations[0]['sql']))
            self.assertFalse(observations[0]['mutationSHAvailable']);self.assertFalse(observations[0]['physicalSHAvailable'])
            self.assertTrue(after[''][0]!=before[''][0],'actual checkpoint must advance the main database image')
            self.assertTrue(wal.exists());self.assertEqual(wal.stat().st_size,0)
            self.assertEqual(row,(body,));self.assertEqual(self.W.pending_count(),0)
    def test_independent_mutation_or_physical_sh_defers_before_checkpoint_sql_without_file_changes(self):
        with self.fixture.writer() as (writer,_component):
            self.commit_wal(writer,'blocked-admission');checkpoint=self.api()
            for name in ('base-mutation.lock','base-physical.lock'):
                with self.subTest(lock=name):
                    sql=[];busy=False;error=None
                    with self.fixture.held(name,'SH'):
                        before=image(self.case.db);writer.db.set_trace_callback(sql.append)
                        try:checkpoint(writer._connection_owner,deadline=time.monotonic()+.03)
                        except self.W.ReaderWindowBusy as exc:busy=True;error=str(exc)
                        finally:writer.db.set_trace_callback(None)
                        after=image(self.case.db)
                    checkpoints=[statement for statement in sql if 'WAL_CHECKPOINT' in statement.upper()]
                    emit('checkpoint-lock-admission',lock=name,error=error,checkpointSql=checkpoints,
                        databaseAndAuxUnchanged=after==before,pending=self.W.pending_count())
                    self.assertTrue(busy);self.assertEqual(checkpoints,[])
                    self.assertTrue(after==before,'admission busy must preserve all main/aux bytes and file identities')
                    self.assertEqual(writer.db.execute('SELECT 1').fetchone(),(1,));self.assertEqual(self.W.pending_count(),0)
    def test_actual_legacy_reader_snapshot_makes_native_checkpoint_partial_or_busy_not_complete(self):
        reader=None
        with self.fixture.writer() as (writer,_component):
            self.commit_wal(writer,'older-snapshot');checkpoint=self.api()
            try:
                reader=sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=.2)
                reader.execute('BEGIN');old_count=reader.execute('SELECT count(*) FROM content').fetchone()[0]
                digest,body,_before_commit=self.commit_wal(writer,'newer-than-pinned-reader')
                if reader.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone() is not None:
                    raise RuntimeError('independent legacy reader did not retain its older actual WAL snapshot')
                before=image(self.case.db);error=None;busy=False;result=None;native_timeouts=[]
                cls=type(writer.db);actual=cls.execute
                def observe_timeout(db,sql,*args,**kwargs):
                    if db is writer.db and fixed(sql):
                        native_timeouts.append(actual(db,'PRAGMA busy_timeout').fetchone()[0])
                    return actual(db,sql,*args,**kwargs)
                start=time.monotonic()
                with mock.patch.object(cls,'execute',new=observe_timeout):
                    try:result=checkpoint(writer._connection_owner,deadline=start+.05)
                    except self.W.ReaderWindowBusy as exc:busy=True;error=str(exc)
                elapsed=time.monotonic()-start;after=image(self.case.db)
                shm_identity=(before['-shm'] is not None and after['-shm'] is not None
                              and before['-shm'][1]==after['-shm'][1])
                emit('actual-legacy-snapshot-checkpoint',error=error,busy=busy,result=result,elapsed=elapsed,
                    oldSnapshotCount=old_count,readerStillOld=reader.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone() is None,
                    mainChangedByPartialCheckpoint=before[''][0]!=after[''][0],
                    walBeforeBytes=len(before['-wal'][0]),walAfterBytes=None if after['-wal'] is None else len(after['-wal'][0]),
                    existingShmIdentityPreserved=shm_identity,nativeBusyTimeoutsAtCheckpoint=native_timeouts,
                    busyTimeout=writer.db.execute('PRAGMA busy_timeout').fetchone()[0])
                self.assertTrue(busy);self.assertIsNone(result,'partial/busy cannot claim reset_complete')
                self.assertEqual(len(native_timeouts),1);self.assertGreaterEqual(native_timeouts[0],0)
                self.assertLessEqual(native_timeouts[0],60,'native busy wait must use the remaining 50ms budget, not default 2000ms')
                self.assertIsNotNone(after['-wal']);self.assertGreater(len(after['-wal'][0]),0)
                self.assertTrue(shm_identity);self.assertEqual(writer.db.execute('PRAGMA busy_timeout').fetchone(),(2000,))
                self.assertEqual(writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone(),(body,))
                reader.rollback();reader.close();reader=None
                self.assertEqual(checkpoint(writer._connection_owner,deadline=time.monotonic()+1),
                                 dict(state='reset_complete',checkpoint=(0,0,0)))
                self.assertEqual(self.W.pending_count(),0)
            finally:
                if reader is not None:reader.rollback();reader.close()
    def test_actual_checkpoint_sql_error_restores_busy_timeout_locks_and_live_owner(self):
        with self.fixture.writer() as (writer,_component):
            digest,body,_before=self.commit_wal(writer,'sql-error');checkpoint=self.api()
            self.assertEqual(writer.db.execute('PRAGMA busy_timeout').fetchone(),(2000,))
            cls=type(writer.db);actual=cls.execute;injected=[];error=None
            def bad_sql(db,sql,*args,**kwargs):
                if db is writer.db and fixed(sql):
                    injected.append(dict(sql=sql,mutationSHAvailable=self.fixture.probe('base-mutation.lock','SH'),
                                         physicalSHAvailable=self.fixture.probe('base-physical.lock','SH')))
                    return actual(db,'SELECT FROM actual_checkpoint_syntax_fault')
                return actual(db,sql,*args,**kwargs)
            with mock.patch.object(cls,'execute',new=bad_sql):
                try:checkpoint(writer._connection_owner,deadline=time.monotonic()+1)
                except (sqlite3.Error,ValueError,RuntimeError) as exc:error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
            timeout=writer.db.execute('PRAGMA busy_timeout').fetchone()[0]
            locks=dict(mutation=self.fixture.probe('base-mutation.lock'),physical=self.fixture.probe('base-physical.lock'))
            emit('actual-checkpoint-sql-error',error=error,injection=injected,busyTimeout=timeout,locksReleased=locks,
                 ownerUsable=writer.db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone()==(body,),
                 pending=self.W.pending_count())
            self.assertEqual(len(injected),1);self.assertIsNotNone(error)
            self.assertFalse(injected[0]['mutationSHAvailable']);self.assertFalse(injected[0]['physicalSHAvailable'])
            self.assertEqual(timeout,2000);self.assertEqual(locks,dict(mutation=True,physical=True))
            self.assertFalse(writer.db.in_transaction);self.assertEqual(writer.db.execute('SELECT 1').fetchone(),(1,))
            self.assertEqual(self.W.pending_count(),0)

    def test_hardlinked_wal_or_shm_refuses_before_checkpoint_and_preserves_alias(self):
        with self.fixture.writer() as (writer,_component):
            checkpoint=self.api()
            for suffix in ('-wal','-shm'):
                with self.subTest(aux=suffix):
                    self.commit_wal(writer,'hardlink-'+suffix)
                    file=Path(str(self.case.db)+suffix);alias=self.case.home/('checkpoint-alias'+suffix)
                    os.link(file,alias)
                    try:
                        before=image(self.case.db);alias_before=alias.read_bytes();sql=[];error=None
                        writer.db.set_trace_callback(sql.append)
                        try:checkpoint(writer._connection_owner,deadline=time.monotonic()+1)
                        except Exception as exc:error=type(exc).__name__+':'+str(exc)
                        finally:writer.db.set_trace_callback(None)
                        checkpoints=[s for s in sql if 'WAL_CHECKPOINT' in s.upper()]
                        after=image(self.case.db)
                        emit('hardlinked-checkpoint-aux',suffix=suffix,error=error,checkpointSql=checkpoints,
                            filesUnchanged=before==after,aliasUnchanged=alias.read_bytes()==alias_before)
                        self.assertIsNotNone(error);self.assertEqual(checkpoints,[])
                        self.assertTrue(before==after);self.assertEqual(alias.read_bytes(),alias_before)
                    finally:alias.unlink()

    def test_empty_replacement_wal_inode_is_not_accepted_as_completed_reset(self):
        with self.fixture.writer() as (writer,_component):
            self.commit_wal(writer,'wal-replacement');checkpoint=self.api()
            wal=Path(str(self.case.db)+'-wal');backup=self.case.home/'checkpoint-original-wal'
            cls=type(writer.db);actual=cls.execute;swaps=[];error=None;result=None
            def swap(db,sql,*args,**kwargs):
                cursor=actual(db,sql,*args,**kwargs)
                if db is writer.db and fixed(sql):
                    old=wal.lstat();wal.rename(backup)
                    fd=os.open(wal,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
                    new=wal.lstat();swaps.append(([old.st_dev,old.st_ino],[new.st_dev,new.st_ino]))
                return cursor
            try:
                with mock.patch.object(cls,'execute',swap):
                    try:result=checkpoint(writer._connection_owner,deadline=time.monotonic()+1)
                    except Exception as exc:error=type(exc).__name__+':'+str(exc)
                emit('checkpoint-wal-inode-replacement',error=error,result=result,swaps=swaps)
                self.assertEqual(len(swaps),1);self.assertNotEqual(*swaps[0])
                self.assertIsNotNone(error);self.assertIsNone(result)
            finally:
                if backup.exists():
                    wal.unlink();backup.rename(wal)

    def test_wal_replaced_before_checkpoint_cannot_truncate_already_open_original(self):
        with self.fixture.writer() as (writer,_component):
            self.commit_wal(writer,'pre-checkpoint-wal-replacement');checkpoint=self.api()
            wal=Path(str(self.case.db)+'-wal');backup=self.case.home/'checkpoint-open-wal'
            original=wal.lstat();wal.rename(backup)
            fd=os.open(wal,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            sql=[];error=None;result=None
            try:
                before=image(self.case.db);original_bytes=backup.read_bytes()
                writer.db.set_trace_callback(sql.append)
                try:result=checkpoint(writer._connection_owner,deadline=time.monotonic()+1)
                except Exception as exc:error=type(exc).__name__+':'+str(exc)
                finally:writer.db.set_trace_callback(None)
                after=image(self.case.db);checkpoints=[s for s in sql if 'WAL_CHECKPOINT' in s.upper()]
                emit('checkpoint-already-open-wal-replacement',error=error,result=result,
                    originalNode=[original.st_dev,original.st_ino],replacementNode=list(before['-wal'][1][:2]),
                    checkpointSql=checkpoints,filesUnchanged=before==after,
                    originalOpenWalUnchanged=backup.read_bytes()==original_bytes)
                self.assertIsNotNone(error);self.assertIsNone(result);self.assertEqual(checkpoints,[])
                self.assertTrue(before==after);self.assertEqual(backup.read_bytes(),original_bytes)
            finally:
                wal.unlink();backup.rename(wal)

if __name__=='__main__':unittest.main(verbosity=2)
