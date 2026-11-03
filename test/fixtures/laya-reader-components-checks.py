#!/usr/bin/env python3
"""Explicit queue/catalog/binding connection windows over a real selected v5."""
from contextlib import ExitStack
import gc
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('components_windows_fixture',ROOT/'test/fixtures/laya-reader-windows-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)
ROLES=('base','queue','catalog','binding')
STORE_ROLES=('queue','catalog','binding')

def emit(case,**fields):print(json.dumps(dict(case=case,**fields),sort_keys=True),flush=True)

class BindingConstructorFailure(RuntimeError):pass

class ReaderComponentChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.ReaderWindowsChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.seed,self.case=self.fixture.seed,self.fixture.case
        self.paths=dict(base=self.case.db,queue=self.case.queue,catalog=self.case.catalog,binding=self.case.store)
        self.roots={self.case.WIKI:str(self.case.auto/'wiki')}
        self.runtime_module=importlib.import_module('laya_index_runtime')
        self.queue_module=importlib.import_module('laya_index_claim_queue')
        self.catalog_module=importlib.import_module('laya_source_catalog')
        self.binding_module=importlib.import_module('laya_index_capacity')
    def feature(self,reason):
        emit('component-feature-absence',actualV5=True,actualPreparedOwner=True,reason=reason)
        self.fail('T3 component feature RED after actual v5/full installer/owner: '+reason)
    def components(self):
        windows=importlib.import_module('laya_reader_windows')
        self.seed.reader._recheck(self.seed.installer.endpoint)
        windows.prepare_windows(self.seed.installer)
        result={}
        for role in ROLES:
            try:result[role]=windows.component_for(self.seed.installer,role)
            except ValueError as exc:
                if str(exc)=='reader_component_unsupported':self.feature('unsupported role '+role)
                raise
            self.assertEqual(result[role].path,self.paths[role])
            self.assertEqual(result[role].install_id,self.seed.install)
        return windows,result
    def require_constructor_api(self,role):
        target={'queue':self.queue_module.ClaimQueue.open,'catalog':self.catalog_module.LayaSourceCatalog,
                'binding':self.binding_module.GenerationBindingStore}[role]
        if 'coordination' not in inspect.signature(target).parameters:self.feature(role+' constructor coordination missing')
    def require_runtime_api(self):
        for role in STORE_ROLES:self.require_constructor_api(role)
        if 'connection_windows' not in inspect.signature(self.runtime_module.LayaIndexRuntime).parameters:
            self.feature('runtime connection_windows missing')
        if 'deadline' not in inspect.signature(self.runtime_module.LayaIndexRuntime.close).parameters:
            self.feature('runtime close overall deadline missing')
    def construct(self,role,component):
        if role=='queue':return self.queue_module.ClaimQueue.open(self.case.queue,coordination=component)
        if role=='catalog':return self.catalog_module.LayaSourceCatalog(self.case.catalog,self.roots,
            self.case.route['scopeId'],wiki_only=True,coordination=component)
        return self.binding_module.GenerationBindingStore(self.case.store,scope=self.case.route['scopeId'],
            db_incarnation=self.case.route['dbIncarnation'],roots=self.roots,coordination=component)
    def runtime(self,components):
        try:return self.runtime_module.LayaIndexRuntime(source_root=self.roots,scope=self.case.route['scopeId'],
            db_path=str(self.case.db),queue_path=str(self.case.queue),db_incarnation=self.case.route['dbIncarnation'],
            catalog_path=str(self.case.catalog),binding_store_path=str(self.case.store),wiki_only=True,
            connection_windows=components)
        except ValueError as exc:
            if str(exc)=='invalid_runtime_connection_windows':self.feature('runtime rejects four-role windows')
            raise
    def observers(self,windows,components,*,fail=None):
        return {role:existing.ConnectionObservation(self.paths[role],windows,components[role],
            fail_first_pragma=role==fail) for role in ROLES}
    def dispatch(self,observers,trace=None):
        actual=sqlite3.connect
        def connect(database,*args,**kwargs):
            path=existing.database_path(database)
            if trace is not None:trace.append(dict(path=str(path),database=str(database)))
            for role,observer in observers.items():
                if path==self.paths[role]:return observer.connect(database,*args,**kwargs)
            return actual(database,*args,**kwargs)
        return connect
    def assert_closed_roles(self,observers,roles):
        for role in roles:
            with self.subTest(closedRole=role):
                self.fixture.assert_closed(observers[role].latest())
                closes=[row for row in observers[role].events if row['event']=='actual-db-close']
                self.assertEqual(len(closes),1);self.assertTrue(closes[0]['physicalExclusive'])
    def drain(self,windows):return windows.drain_pending(deadline=time.monotonic()+1)

    def test_four_role_runtime_gc_retains_all_deferred_connections_until_same_thread_drain(self):
        windows=None;runtime=None;external=ExitStack();observers=None
        try:
            with self.seed.prepared():
                windows,components=self.components();self.require_runtime_api()
                observers=self.observers(windows,components)
                with mock.patch.object(sqlite3,'connect',side_effect=self.dispatch(observers)):
                    runtime=self.runtime(components)
                references={role:observers[role].latest() for role in ROLES}
                for role in ROLES:self.assertEqual(len(observers[role].refs),1,role+' actual coordinated RW required')
                for role in sorted(components):external.enter_context(windows.physical_window(
                    components[role],exclusive=False,deadline=time.monotonic()+0.2))
                with self.assertRaises(windows.ReaderCloseDeferred):runtime.close(deadline=time.monotonic()+0.03)
                runtime=None;gc.collect()
                alive={role:self.fixture.live(reference) for role,reference in references.items()}
                closes={role:sum(row['event']=='actual-db-close' for row in observers[role].events) for role in ROLES}
                emit('four-role-runtime-close-deferred',pending=windows.pending_count(),aliveAfterGc=alive,
                     actualCloseCounts=closes,testStoresOnlyWeakConnectionReferences=True)
                self.assertEqual(windows.pending_count(),4);self.assertTrue(all(alive.values()))
                self.assertEqual(closes,{role:0 for role in ROLES})
            busy=None
            try:
                with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
            except ValueError as exc:busy=str(exc)
            self.assertEqual(busy,'reader_lifetime_busy')
            external.close();self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed_roles(observers,ROLES)
            with self.seed.lease.reader_lifetime(self.seed.install,exclusive=True):pass
        finally:
            external.close()
            if runtime is not None:runtime.close(deadline=time.monotonic()+1)
            if windows is not None:self.drain(windows)

    def test_each_store_first_rw_pragma_failure_closes_under_actual_physical_exclusive(self):
        with self.seed.prepared():
            windows,components=self.components()
            for role in STORE_ROLES:
                with self.subTest(role=role):
                    self.require_constructor_api(role)
                    observer=self.observers(windows,components,fail=role)[role]
                    opened=None;error=None
                    try:
                        with windows.physical_window(components[role],exclusive=False,deadline=time.monotonic()+0.2):pass
                        with mock.patch.object(sqlite3,'connect',side_effect=self.dispatch({role:observer})):
                            try:opened=self.construct(role,components[role])
                            except Exception as exc:error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                        gc.collect()
                        emit('store-first-pragma-failure',role=role,error=error,returned=opened is not None,
                             injected=observer.injected,events=observer.events,pending=windows.pending_count())
                        self.assertTrue(observer.injected);self.assertIsNotNone(error);self.assertIsNone(opened)
                        failures=[row for row in observer.events if row['event']=='first-pragma-failure']
                        self.assertEqual(len(failures),1);self.assertTrue(failures[0]['physicalExclusive'])
                        self.assert_closed_roles({role:observer},(role,));self.assertEqual(windows.pending_count(),0)
                        with windows.physical_window(components[role],exclusive=True,deadline=time.monotonic()+0.2):pass
                    finally:
                        if opened is not None:opened.close(deadline=time.monotonic()+1)
                        self.drain(windows)

    def test_binding_constructor_failure_keeps_all_three_prior_runtime_handles_pending(self):
        windows=None;external=ExitStack();observers=None;runtime=None
        try:
            with self.seed.prepared():
                windows,components=self.components();self.require_runtime_api()
                observers=self.observers(windows,components)
                prior=('base','queue','catalog')
                def fail_binding(*args,**kwargs):
                    for role in prior:self.assertEqual(len(observers[role].refs),1,role+' must already be coordinated')
                    for role in sorted(prior):external.enter_context(windows.physical_window(
                        components[role],exclusive=False,deadline=time.monotonic()+0.2))
                    raise BindingConstructorFailure('synthetic_binding_constructor_failure')
                error=None
                with mock.patch.object(sqlite3,'connect',side_effect=self.dispatch(observers)), \
                        mock.patch.object(self.binding_module.GenerationBindingStore,'__init__',new=fail_binding):
                    try:runtime=self.runtime(components)
                    except (BindingConstructorFailure,windows.ReaderCloseDeferred) as exc:
                        error=type(exc).__name__+':'+str(exc);exc.__traceback__=None
                gc.collect()
                alive={role:self.fixture.live(observers[role].latest()) for role in prior}
                closes={role:sum(row['event']=='actual-db-close' for row in observers[role].events) for role in prior}
                emit('runtime-binding-constructor-failure',error=error,returned=runtime is not None,
                     pending=windows.pending_count(),priorConnectionsAliveAfterGc=alive,actualCloseCounts=closes)
                self.assertIsNone(runtime);self.assertIsNotNone(error);self.assertEqual(windows.pending_count(),3)
                self.assertTrue(all(alive.values()));self.assertEqual(closes,{role:0 for role in prior})
            external.close();self.assertEqual(self.drain(windows),0);self.assertEqual(windows.pending_count(),0)
            self.assert_closed_roles(observers,prior)
        finally:
            external.close()
            if runtime is not None:runtime.close(deadline=time.monotonic()+1)
            if windows is not None:self.drain(windows)

    def test_store_physical_shared_lock_blocks_the_target_first_sqlite_open(self):
        with self.seed.prepared():
            windows,components=self.components()
            for role in STORE_ROLES:
                with self.subTest(role=role):
                    self.require_constructor_api(role);observed=[];opened=None;error=None
                    with windows.physical_window(components[role],exclusive=False,deadline=time.monotonic()+0.2):
                        started=time.monotonic()
                        try:
                            with mock.patch.object(sqlite3,'connect',side_effect=self.dispatch({},observed)):
                                opened=self.construct(role,components[role])
                        except windows.ReaderWindowBusy as exc:error=str(exc)
                        finally:
                            target=[row for row in observed if row['path']==str(self.paths[role])]
                            emit('store-first-open-blocked',role=role,error=error,elapsed=time.monotonic()-started,
                                 targetConnects=target,pending=windows.pending_count())
                    try:
                        self.assertIsNotNone(error);self.assertIsNone(opened)
                        self.assertEqual(target,[],'target must acquire physical EX before even its first RO validation')
                        self.assertEqual(windows.pending_count(),0)
                    finally:
                        if opened is not None:opened.close(deadline=time.monotonic()+1)
                        self.drain(windows)

if __name__=='__main__':unittest.main(verbosity=2)
