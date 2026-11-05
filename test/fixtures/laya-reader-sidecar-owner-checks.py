#!/usr/bin/env python3
"""Existing completed-sidecar physical owner; no coordinated creation claim."""
from contextlib import ExitStack
import gc
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sidecar_components_fixture',ROOT/'test/fixtures/laya-reader-components-checks.py')
components_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(components_fixture)
existing=components_fixture.existing

def emit(case,**values):print(json.dumps(dict(case=case,**values),sort_keys=True),flush=True)

class SidecarOwnerChecks(unittest.TestCase):
    def setUp(self):
        self.tools=None
        if self._testMethodName=='test_sealed_absent_sidecar_refuses_coordinated_creation_before_first_content_open':
            self.seed=existing.seed_module.MoveSealChecks('runTest')
            self.seed.PUBLISH_SIDECAR=False
            self.addCleanup(self.seed.doCleanups);self.seed.setUp()
        else:
            self.tools=components_fixture.ReaderComponentChecks('runTest')
            self.addCleanup(self.tools.doCleanups);self.tools.setUp();self.seed=self.tools.seed
        self.case=self.seed.case
        self.incremental=importlib.import_module('laya_incremental_docs')
        self.windows=importlib.import_module('laya_reader_windows')
        self.sidecar=self.case.state/self.incremental.FILE
        build=importlib.import_module('laya_initial_build')
        self.identity=build.read(self.case.state)['identity']
    def feature(self,reason):
        emit('sidecar-owner-feature-absence',actualV5=True,actualPreparedOwner=True,
             sidecarPresent=self.sidecar.exists(),reason=reason)
        self.fail('T3 sidecar feature RED after actual v5/full installer/owner: '+reason)
    def component(self):
        self.seed.reader._recheck(self.seed.installer.endpoint)
        self.windows.prepare_windows(self.seed.installer)
        try:component=self.windows.component_for(self.seed.installer,'sidecar')
        except ValueError as exc:
            if str(exc) in ('reader_component_identity_missing','reader_component_unsupported'):
                self.feature(str(exc))
            raise
        self.assertEqual(component.path,self.sidecar)
        self.assertEqual(component.install_id,self.seed.install)
        self.assertEqual(type(component).__module__,self.windows.__name__)
        for name in ('coordination','identity'):
            if name not in inspect.signature(self.incremental._open).parameters:self.feature('_open '+name+' missing')
        if not callable(getattr(self.incremental,'_close',None)):self.feature('_close missing')
        if not callable(getattr(self.windows,'close_connection',None)):self.feature('close_connection missing')
        return component
    def drain(self):return self.windows.drain_pending(deadline=time.monotonic()+1)
    def close_events(self,observer):return [row for row in observer.events if row['event']=='actual-db-close']
    def lifetime_busy(self):
        error=None
        try:
            with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
        except ValueError as exc:error=str(exc)
        return error
    def assert_closed(self,observer):
        existing.ReaderWindowsChecks.assert_closed(self,observer.latest())
        closes=self.close_events(observer)
        self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])

    def test_existing_sidecar_raw_connection_defer_survives_gc_and_outer_owner(self):
        db=None;external=ExitStack();observer=None
        try:
            with self.seed.prepared():
                component=self.component()
                observer=existing.ConnectionObservation(self.sidecar,self.windows,component)
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    db=self.incremental._open(self.case.state,coordination=component,identity=self.identity)
                self.assertIsInstance(db,sqlite3.Connection)
                self.assertEqual(db.execute('SELECT count(*) FROM docs').fetchone(),(3,))
                reference=observer.latest()
                external.enter_context(self.windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2))
                with self.assertRaises(self.windows.ReaderCloseDeferred):
                    self.incremental._close(db,deadline=time.monotonic()+0.03)
                db=None;gc.collect()
                emit('sidecar-independent-pending-owner',pending=self.windows.pending_count(),
                     connectionAliveAfterGc=existing.ReaderWindowsChecks.live(reference),events=observer.events)
                self.assertEqual(self.windows.pending_count(),1)
                self.assertTrue(existing.ReaderWindowsChecks.live(reference));self.assertEqual(self.close_events(observer),[])
            self.assertEqual(self.lifetime_busy(),'reader_lifetime_busy')
            external.close()
            self.assertEqual(self.lifetime_busy(),'reader_lifetime_busy','pending handle owns its own lifetime SH after reader SH release')
            self.assertEqual(self.drain(),0);self.assertEqual(self.windows.pending_count(),0)
            self.assert_closed(observer);self.assertIsNone(self.lifetime_busy())
        finally:
            external.close()
            if db is not None:self.incremental._close(db,deadline=time.monotonic()+1)
            self.drain()

    def test_actual_publish_document_finally_defers_sidecar_while_runtime_closes_independently(self):
        runtime=None;external=ExitStack();observer=None
        try:
            with self.seed.prepared():
                component=self.component()
                if 'coordination' not in inspect.signature(self.incremental.publish_document).parameters:
                    self.feature('publish_document coordination missing')
                self.tools.require_runtime_api();_windows,components=self.tools.components()
                runtime=self.tools.runtime(components)
                snapshot=runtime.catalog.require_complete()
                with self.seed.ro(self.sidecar) as old:
                    row=old.execute('SELECT path,hash,title,body,profile_id FROM docs ORDER BY path LIMIT 1').fetchone()
                if row is None:raise RuntimeError('actual existing completed document prerequisite missing')
                original_setup=self.incremental._setup;setups=[]
                observer=existing.ConnectionObservation(self.sidecar,self.windows,component)
                def setup_then_hold(db,identity,**kwargs):
                    result=original_setup(db,identity,**kwargs)
                    if kwargs.get('coordination') is component:
                        setups.append(True)
                        external.enter_context(self.windows.physical_window(component,exclusive=False,
                            deadline=time.monotonic()+0.2))
                    return result
                error=None;returned=None;deferred=False
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect), \
                        mock.patch.object(self.incremental,'_setup',new=setup_then_hold):
                    try:returned=self.incremental.publish_document(runtime,self.identity,snapshot,*row,
                        require_verified=False,coordination=component)
                    except self.windows.ReaderCloseDeferred as exc:
                        error=str(exc);deferred=True;exc.__traceback__=None
                gc.collect()
                reference=observer.latest()
                runtime_error=None
                try:runtime.close(deadline=time.monotonic()+1)
                except (ValueError,OSError,RuntimeError) as exc:runtime_error=str(exc)
                if runtime_error is None:runtime=None
                emit('actual-publish-document-finally-owner',actualSetupCalls=len(setups),error=error,
                    closeDeferred=deferred,returned=returned,runtimeCloseError=runtime_error,
                    sidecarPendingAfterRuntimeClose=self.windows.pending_count(),
                    sidecarAliveAfterGc=existing.ReaderWindowsChecks.live(reference),events=observer.events,
                    documentAndVectorCachePreconditionsActual=True)
                self.assertEqual(len(setups),1);self.assertTrue(deferred);self.assertIsNone(returned)
                self.assertIsNone(runtime_error);self.assertEqual(self.windows.pending_count(),1)
                self.assertTrue(existing.ReaderWindowsChecks.live(reference));self.assertEqual(self.close_events(observer),[])
            external.close()
            self.assertEqual(self.lifetime_busy(),'reader_lifetime_busy')
            self.assertEqual(self.drain(),0);self.assertEqual(self.windows.pending_count(),0)
            self.assert_closed(observer);self.assertIsNone(self.lifetime_busy())
        finally:
            external.close()
            if runtime is not None:runtime.close(deadline=time.monotonic()+1)
            self.drain()

    def test_existing_sidecar_first_rw_pragma_failure_closes_under_physical_exclusive(self):
        db=None
        try:
            with self.seed.prepared():
                component=self.component()
                with self.windows.physical_window(component,exclusive=False,deadline=time.monotonic()+0.2):pass
                observer=existing.ConnectionObservation(self.sidecar,self.windows,component,fail_first_pragma=True)
                error=None
                with mock.patch.object(sqlite3,'connect',side_effect=observer.connect):
                    try:db=self.incremental._open(self.case.state,coordination=component,identity=self.identity)
                    except Exception as exc:error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                gc.collect()
                emit('sidecar-first-pragma-failure',error=error,returned=db is not None,injected=observer.injected,
                     events=observer.events,pending=self.windows.pending_count())
                self.assertTrue(observer.injected);self.assertIsNotNone(error);self.assertIsNone(db)
                failures=[row for row in observer.events if row['event']=='first-pragma-failure']
                self.assertEqual(len(failures),1);self.assertTrue(failures[0]['physicalExclusive'])
                self.assert_closed(observer);self.assertEqual(self.windows.pending_count(),0)
                with self.windows.physical_window(component,exclusive=True,deadline=time.monotonic()+0.2):pass
        finally:
            if db is not None:self.incremental._close(db,deadline=time.monotonic()+1)
            self.drain()

    def test_sealed_absent_sidecar_refuses_coordinated_creation_before_first_content_open(self):
        self.assertFalse(os.path.lexists(self.sidecar))
        with self.seed.prepared():
            component=self.component()
            before=existing.ReaderWindowsChecks.files(self.sidecar)
            actual=sqlite3.connect;trace=[];db=None;error=None;invalid=False
            def observe(database,*args,**kwargs):
                path=existing.database_path(database)
                if path.is_absolute() and self.case.state in path.parents and path!=self.case.journal:
                    trace.append(str(database))
                return actual(database,*args,**kwargs)
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=observe):
                    try:db=self.incremental._open(self.case.state,coordination=component,identity=self.identity)
                    except Exception as exc:
                        error=str(exc);invalid=isinstance(exc,ValueError);exc.__traceback__=None
            finally:
                if db is not None:self.incremental._close(db,deadline=time.monotonic()+1)
                self.drain()
            emit('sealed-sidecar-absence',error=error,valueError=invalid,returned=db is not None,
                contentConnects=trace,sidecarCreated=os.path.lexists(self.sidecar),
                sidecarAndAuxPreserved=existing.ReaderWindowsChecks.files(self.sidecar)==before,
                pending=self.windows.pending_count(),actualSeedNeverPublishedSidecar=True)
            self.assertTrue(invalid)
            self.assertIn(error,('reader_component_absent','incremental_coordinated_creation_unavailable'))
            self.assertIsNone(db);self.assertEqual(trace,[]);self.assertFalse(os.path.lexists(self.sidecar))
            self.assertTrue(existing.ReaderWindowsChecks.files(self.sidecar)==before,'absent main/aux must remain absent')
            self.assertEqual(self.windows.pending_count(),0)

if __name__=='__main__':unittest.main(verbosity=2)
