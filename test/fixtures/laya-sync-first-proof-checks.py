#!/usr/bin/env python3
"""Actual selected worker first proof; later worker opens remain separate."""
from contextlib import ExitStack, closing
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sync_first_seed',ROOT/'test/fixtures/laya-normal-writer-connections-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

def emit(label,**values):print(json.dumps(dict(case=label,**values),sort_keys=True),flush=True)

class SyncFirstProofChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.NormalWriterConnectionChecks('runTest')
        self.addCleanup(self.fixture.doCleanups)
        reader=importlib.import_module('laya_journal_reader')
        convert=reader.convert_reader_v5;self.v4=None
        def observed(state,*args,**kwargs):
            path=Path(state)/'install-journal.sqlite'
            self.v4=path.read_bytes()
            return convert(state,*args,**kwargs)
        with mock.patch.object(reader,'convert_reader_v5',side_effect=observed):self.fixture.setUp()
        for name in ('case','seed','W','P','J','sync','queue'):
            setattr(self,name,getattr(self.fixture,name))
        self.worker=importlib.import_module('laya_product_sync_worker')
        # Synthetic executing-bundle authority, consistent with the seed's
        # actual fullproof. Product default derives its own module root.
        root=mock.patch.object(self.worker,'_writer_executing_root',create=True,return_value=self.case.plugin)
        root.start();self.addCleanup(root.stop)

    def selected(self):return self.worker._selected(self.case.project)

    def trace(self,calls):return self.fixture.fixture.fixture.trace(calls)

    def deferred_run(self):
        error=None;result=None;starts=[]
        def forbidden(*args,**kwargs):starts.append(True);raise AssertionError('model/provider reached before initial defer')
        with mock.patch.object(self.worker,'ReviewedLocalQmdProvider',side_effect=forbidden), \
                mock.patch.object(self.worker,'Coordinator',side_effect=forbidden):
            try:result=self.worker.run(self.case.project)
            except Exception as exc:error=type(exc).__name__+':'+str(exc)
        return result,error,starts

    def test_actual_selected_first_content_proof_uses_owned_ro_and_ex(self):
        with self.fixture.F.warm(),self.seed.authority():
            calls=[];closes=[];native=self.W._OwnedSQLite._native_close_locked
            def close(db):
                closes.append(not self.fixture.F.fixture.probe('base-physical.lock','SH'))
                return native(db)
            with self.trace(calls),mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                decision,proof=self.selected()
            emit('worker-first-owned-proof',contentConnects=calls,closeUnderEX=closes)
            self.assertTrue(proof['productRouteAllowed']);self.assertEqual(decision['projectRoot'],str(self.case.project))
            self.assertEqual(len(calls),2);self.assertTrue(all(c['managed'] for c in calls))
            self.assertEqual(closes,[True,True])

    def test_busy_owner_defers_before_content_and_provider_without_initial_build_fallback(self):
        with self.fixture.F.warm(),self.seed.authority(),self.sync.sync_owner(self.queue) as owner:
            self.assertIsNotNone(owner);calls=[]
            with self.trace(calls):result,error,starts=self.deferred_run()
            emit('worker-first-owner-busy',result=result,error=error,contentConnects=calls,providerStarts=len(starts))
            self.assertIsNone(error);self.assertEqual(result,{'state':'deferred','reason':'owner_busy'})
            self.assertEqual(calls,[]);self.assertEqual(starts,[])

    def test_missing_prepared_manifest_defers_without_sqlite_or_namespace_repair(self):
        with self.fixture.F.warm(),self.seed.authority():
            parent=self.fixture.F.fixture.parent
            manifest=parent/self.W._MANIFEST;saved=parent/'saved-prepared-manifest'
            os.rename(manifest,saved);before=self.fixture.F.fixture.fixture.snapshot(parent)
            calls=[]
            try:
                with self.trace(calls):result,error,starts=self.deferred_run()
                emit('worker-first-unprepared',result=result,error=error,contentConnects=calls,providerStarts=len(starts))
                self.assertIsNone(error)
                self.assertEqual(result,{'state':'deferred','reason':'writer_windows_not_prepared'})
                self.assertEqual(calls,[]);self.assertEqual(starts,[])
                self.assertEqual(self.fixture.F.fixture.fixture.snapshot(parent),before)
            finally:os.rename(saved,manifest)

    def test_true_no_aux_first_open_defers_without_creating_sidecars(self):
        with self.seed.authority():
            from laya_index_a import LayaIndexA
            from laya_index_claim_queue import ClaimQueue
            # The seed's earlier RO proof can leave warm auxiliaries. Only
            # actual last-writer close establishes this cold baseline.
            with closing(LayaIndexA(self.case.db)),closing(ClaimQueue.open(self.queue)):pass
            self.assertFalse(Path(str(self.case.db)+'-wal').exists())
            files=existing.existing.existing.existing.files
            before={str(p):files(p) for p in (self.case.db,self.queue)};calls=[]
            from resource_deferred import ResourceDeferred
            error=None
            with self.trace(calls):
                try:self.selected()
                except Exception as exc:error=exc
            emit('worker-first-cold-refusal',error=None if error is None else str(error),contentConnects=calls)
            self.assertIsInstance(error,ResourceDeferred)
            self.assertEqual(str(error),'writer_proof_cold_open_unproved')
            self.assertEqual(calls,[]);self.assertEqual({str(p):files(p) for p in (self.case.db,self.queue)},before)

    def test_actual_v4_to_v5_image_publication_between_version_and_proof_retries_before_raw_content(self):
        with self.fixture.F.warm(),self.seed.authority():
            self.assertIsNotNone(self.v4)
            v5=self.case.journal.read_bytes()
            def publish(body):
                tmp=self.case.journal.with_name('test-first-proof-journal-image')
                tmp.write_bytes(body);tmp.chmod(0o600);os.replace(tmp,self.case.journal)
            publish(self.v4)
            self.assertEqual(self.J.journal_version(self.case.journal),4)
            version=self.J.journal_version;observed=[]
            def transition(path):
                result=version(path);observed.append(result)
                if result==4:publish(v5)
                return result
            calls=[]
            with self.trace(calls),mock.patch.object(self.J,'journal_version',side_effect=transition):
                _decision,proof=self.selected()
            emit('worker-first-v4-v5-race',observedVersions=observed,contentConnects=calls)
            self.assertIn(4,observed);self.assertTrue(proof['productRouteAllowed'])
            self.assertEqual(len(calls),2);self.assertTrue(all(c['managed'] for c in calls))

    def test_failed_first_native_close_propagates_and_creator_drain_keeps_pin_until_close(self):
        with self.fixture.F.warm(),self.seed.authority():
            readers=importlib.import_module('laya_writer_proof_reader')
            blocked=[True];attempts=[];native=self.W._OwnedSQLite._native_close_locked
            def close(db):
                attempts.append(True)
                if blocked[0]:raise sqlite3.OperationalError('selected-first-proof-native-close-failure')
                return native(db)
            error=None
            try:
                with mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    try:self.selected()
                    except Exception as exc:error=exc
                    self.assertTrue(attempts,'actual first proof must use native-owned readers')
                    self.assertIsNotNone(error)
                    self.assertNotIsInstance(error,importlib.import_module('resource_deferred').ResourceDeferred)
                    self.assertGreater(readers.pending_proof_readers(),0)
                    with self.sync.sync_owner(self.queue) as busy:self.assertIsNone(busy)
                    blocked[0]=False
                    self.assertEqual(readers.drain_proof_readers(deadline=time.monotonic()+2),0)
                with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
                emit('worker-first-native-close-failure',propagated=True,pinUntilCreatorDrain=True)
            finally:
                blocked[0]=False;readers.drain_proof_readers(deadline=time.monotonic()+2)

if __name__=='__main__':unittest.main()
