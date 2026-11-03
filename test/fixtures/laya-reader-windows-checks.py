#!/usr/bin/env python3
"""Base physical open/close contracts; real SQLite and weak observations only."""
from contextlib import ExitStack, closing
import gc
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest import mock
from urllib.parse import unquote,urlparse
import weakref

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('windows_real_seed',ROOT/'test/fixtures/laya-reader-move-seal-checks.py')
seed_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed_module)

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)

def database_path(raw):
    raw=str(raw)
    return Path(unquote(urlparse(raw).path)) if raw.startswith('file:') else Path(raw)

class QueueConstructorFailure(RuntimeError):pass

class ConnectionObservation:
    """Observes actual Connection calls; never retains a connection strongly."""
    def __init__(self,path,windows,component,*,fail_first_pragma=False):
        self.path=Path(path);self.windows=windows;self.component=component
        self.refs=[];self.events=[];self.fail_first_pragma=fail_first_pragma;self.injected=False
        self.actual_connect=sqlite3.connect
        owner=self
        class ObservedConnection(sqlite3.Connection):
            def __init__(self,database,*args,**kwargs):
                super().__init__(database,*args,**kwargs)
                self.is_base_rw=(database_path(database)==owner.path and 'mode=rw' in str(database))
                if self.is_base_rw:owner.refs.append(weakref.ref(self))
            def execute(self,sql,*args,**kwargs):
                if (self.is_base_rw and owner.fail_first_pragma and not owner.injected
                        and sql.strip().upper().startswith('PRAGMA')):
                    owner.injected=True
                    owner.events.append(dict(event='first-pragma-failure',physicalExclusive=owner.exclusive_held()))
                    raise sqlite3.OperationalError('synthetic_first_pragma_failure')
                return super().execute(sql,*args,**kwargs)
            def close(self):
                held=owner.exclusive_held() if self.is_base_rw else None
                super().close()
                if self.is_base_rw:owner.events.append(dict(event='actual-db-close',physicalExclusive=held))
        self.factory=ObservedConnection
    def connect(self,*args,**kwargs):
        kwargs['factory']=self.factory
        return self.actual_connect(*args,**kwargs)
    def exclusive_held(self):
        # Valid component controls before/after construction distinguish the
        # competing SH refusal here from a malformed component/path rejection.
        try:
            with self.windows.physical_window(self.component,exclusive=False,deadline=time.monotonic()+0.03):
                return False
        except Exception as exc:
            text=(type(exc).__name__+':'+str(exc)).lower()
            if 'busy' in text or 'timeout' in text or 'deadline' in text:return True
            raise
    def latest(self):
        if not self.refs:raise RuntimeError('actual base RW connection observation missing')
        return self.refs[-1]

class ReaderWindowsChecks(unittest.TestCase):
    def setUp(self):
        self.seed=seed_module.MoveSealChecks('runTest')
        self.addCleanup(self.seed.doCleanups);self.seed.setUp()
        self.case=self.seed.case
    def api(self):
        if importlib.util.find_spec('laya_reader_windows') is None:
            emit('windows-feature-absence',actualV5=True,actualPublishedCards=3,actualPreparedOwner=True,
                 required='laya_reader_windows',classification='new-T3-feature-not-implemented')
            self.fail('T3 feature RED after real v5/owner proof: laya_reader_windows is absent')
        windows=importlib.import_module('laya_reader_windows')
        for name in ('prepare_windows','component_for','physical_window','pending_count','drain_pending'):
            self.assertTrue(callable(getattr(windows,name,None)),name+' API required')
        self.assertTrue(isinstance(getattr(windows,'ReaderCloseDeferred',None),type))
        windows.prepare_windows(self.seed.installer)
        component=windows.component_for(self.seed.installer,'base')
        self.assertEqual(type(component).__module__,windows.__name__)
        self.assertEqual(windows.pending_count(),0)
        return windows,component
    @staticmethod
    def live(reference):
        if reference() is None:return False
        try:return reference().execute('SELECT 1').fetchone()==(1,)
        except sqlite3.ProgrammingError:return False
    def assert_closed(self,reference):
        if reference() is not None:
            with self.assertRaises(sqlite3.ProgrammingError):reference().execute('SELECT 1')
    @staticmethod
    def drain(windows):
        return windows.drain_pending(deadline=time.monotonic()+1.0)

    def test_uncheckpointed_wal_reader_defers_close_and_gc_keeps_strong_owner(self):
        windows=None;writer=None;reader_db=None;external=ExitStack();reference=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):pass
                with self.assertRaises((TypeError,ValueError,RuntimeError)):
                    with windows.physical_window({'kind':'base','path':str(self.case.db)},
                                                 exclusive=False,deadline=time.monotonic()+0.1):pass
                with self.assertRaises((TypeError,ValueError,RuntimeError)):type(component)()
                observer=ConnectionObservation(self.case.db,windows,component)
                from laya_index_a import LayaIndexA
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    writer=LayaIndexA(self.case.db,coordination=component)
                reference=observer.latest()
                main_before=self.case.db.read_bytes()
                body='synthetic windows committed WAL sentinel'
                digest=hashlib.sha256(body.encode('utf8')).hexdigest()
                writer.ensure_content(digest,body)
                wal=Path(str(self.case.db)+'-wal')
                self.assertTrue(wal.is_file());self.assertGreater(wal.stat().st_size,0)
                self.assertEqual(self.case.db.read_bytes(),main_before,'sentinel must remain an uncheckpointed WAL commit')
                external.enter_context(windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2))
                reader_db=sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True,isolation_level=None)
                self.assertEqual(reader_db.execute('SELECT body FROM content WHERE hash=?',(digest,)).fetchone(),(body,))
                with self.assertRaises(windows.ReaderCloseDeferred):writer.close(deadline=time.monotonic()+0.03)
                writer=None;gc.collect()
                self.assertTrue(self.live(reference),'module must strongly retain the actual connection after caller GC')
                self.assertEqual(windows.pending_count(),1)
                self.assertFalse(any(row['event']=='actual-db-close' for row in observer.events))
            # Actual mover owner/context is gone. The handle's independent
            # lifetime SH must survive until its deferred physical close.
            lifetime_busy=None
            try:
                with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
            except ValueError as exc:lifetime_busy=str(exc)
            emit('wal-close-owner',pending=windows.pending_count(),connectionAliveAfterGc=self.live(reference),
                 lifetimeAfterOuterOwner=lifetime_busy,actualCloseEvents=observer.events,walExists=wal.exists())
            self.assertEqual(lifetime_busy,'reader_lifetime_busy')
            reader_db.close();reader_db=None;external.close()
            self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed(reference)
            closes=[row for row in observer.events if row['event']=='actual-db-close']
            self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
            with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
        finally:
            if reader_db is not None:reader_db.close()
            external.close()
            if writer is not None:
                try:writer.close(deadline=time.monotonic()+0.5)
                except Exception:pass
            if windows is not None:self.drain(windows)

    def test_runtime_queue_constructor_failure_retains_pending_base_after_gc(self):
        windows=None;external=ExitStack();observer=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                observer=ConnectionObservation(self.case.db,windows,component)
                runtime_module=importlib.import_module('laya_index_runtime')
                def queue_failure(*args,**kwargs):
                    # The callback retains only a weak observation, never an
                    # index/runtime/db strong reference from the failed ctor.
                    observer.latest()
                    external.enter_context(windows.physical_window(component,exclusive=False,
                        deadline=time.monotonic()+0.2))
                    raise QueueConstructorFailure('synthetic_queue_constructor_failure')
                error=None;returned=False
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect), \
                        mock.patch.object(runtime_module.ClaimQueue,'open',side_effect=queue_failure):
                    try:
                        runtime_module.LayaIndexRuntime(source_root={self.case.WIKI:str(self.case.auto/'wiki')},
                            scope=self.case.route['scopeId'],db_path=str(self.case.db),queue_path=str(self.case.queue),
                            db_incarnation=self.case.route['dbIncarnation'],wiki_only=True,
                            connection_windows={'base':component})
                        returned=True
                    except (QueueConstructorFailure,windows.ReaderCloseDeferred) as exc:
                        error=type(exc).__name__+':'+str(exc)
                        exc.__traceback__=None
                gc.collect();reference=observer.latest()
                emit('runtime-partial-failure',error=error,returned=returned,pending=windows.pending_count(),
                    connectionAliveAfterGc=self.live(reference),actualCloseEvents=observer.events,
                    testOwnsOnlyWeakConnectionReferences=True)
                self.assertFalse(returned);self.assertIsNotNone(error)
                self.assertEqual(windows.pending_count(),1)
                self.assertTrue(self.live(reference),'failed runtime must not rely on its traceback/GC to retain base')
                self.assertFalse(any(row['event']=='actual-db-close' for row in observer.events))
            external.close()
            self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed(reference)
            closes=[row for row in observer.events if row['event']=='actual-db-close']
            self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
        finally:
            external.close()
            if windows is not None:self.drain(windows)

    def test_first_pragma_failure_closes_actual_connection_under_physical_exclusive(self):
        windows=None
        try:
            with self.seed.prepared():
                windows,component=self.api()
                with windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):pass
                observer=ConnectionObservation(self.case.db,windows,component,fail_first_pragma=True)
                from laya_index_a import LayaIndexA
                error=None;returned=False
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    try:
                        LayaIndexA(self.case.db,coordination=component);returned=True
                    except (sqlite3.OperationalError,ValueError,RuntimeError) as exc:
                        error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                gc.collect()
                emit('first-pragma-init-failure',error=error,returned=returned,injected=observer.injected,
                     actualConnectionEvents=observer.events,pending=windows.pending_count(),
                     activeOwnerRegistryPrivateStateInspected=False)
                self.assertFalse(returned);self.assertTrue(observer.injected)
                self.assertIsNotNone(error);self.assertIn('synthetic_first_pragma_failure',error)
                failures=[row for row in observer.events if row['event']=='first-pragma-failure']
                closes=[row for row in observer.events if row['event']=='actual-db-close']
                self.assertEqual(len(failures),1);self.assertTrue(failures[0]['physicalExclusive'])
                self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
                self.assert_closed(observer.latest());self.assertEqual(windows.pending_count(),0)
                with windows.physical_window(component,exclusive=True,deadline=time.monotonic()+0.2):pass
        finally:
            if windows is not None:self.drain(windows)

if __name__=='__main__':unittest.main(verbosity=2)
