#!/usr/bin/env python3
"""Normal first-write epoch refresh and journal-image CAS preconditions."""
from contextlib import ExitStack, closing, contextmanager
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('normal_intent_seed',ROOT/'test/fixtures/laya-normal-writer-connections-checks.py')
existing = importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

def emit(label,**values):print(json.dumps(dict(case=label,**values),sort_keys=True),flush=True)

class NormalWriterIntentChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.NormalWriterConnectionChecks('runTest')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        for name in ('case','seed','N','W','P','J','sync','queue'):
            setattr(self,name,getattr(self.fixture,name))

    def api(self):
        function=getattr(self.N,'mark_new_write',None)
        if not callable(function):
            emit('normal-intent-feature-absence',actualNormalRuntime=True)
            self.fail('normal first-write epoch refresh absent after real managed runtime baseline')
        return function

    @contextmanager
    def warm_clean(self):
        from laya_index_a import LayaIndexA
        from laya_index_claim_queue import ClaimQueue
        with closing(LayaIndexA(self.case.db)),closing(ClaimQueue.open(self.queue)):
            for path in (self.case.db,self.queue):
                self.assertTrue(Path(str(path)+'-wal').exists())
                self.assertTrue(Path(str(path)+'-shm').exists())
            yield

    def test_actual_mark_refreshes_same_components_and_repeated_call_has_no_cas(self):
        with self.warm_clean(),self.seed.authority(),ExitStack() as owners:
            writer,_context=self.fixture.admission(owners)
            runtime=self.fixture.runtime(writer)
            try:
                mark=self.api();before=self.J.inspect_journal(self.case.journal);calls=[]
                with self.fixture.fixture.fixture.trace(calls):proof=mark(writer)
                after=self.J.inspect_journal(self.case.journal)
                self.assertEqual(after['epoch'],before['epoch']+1);self.assertTrue(after['newWriteSeen'])
                self.assertEqual(proof['selectionEpoch'],after['epoch']);self.assertTrue(proof['productRouteAllowed'])
                self.N.validate_normal_writer(writer)
                self.assertEqual(runtime.index.exclude(writer.scope_id,path='normal-intent-write.md'),'excluded')
                calls_again=[]
                with self.fixture.fixture.fixture.trace(calls_again):again=mark(writer)
                self.assertEqual(again,proof);self.assertEqual(calls_again,[])
                self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'],after['epoch'])
                self.assertEqual(len(calls),4)
                emit('normal-intent-refresh',epochBefore=before['epoch'],epochAfter=after['epoch'],
                     contentConnects=len(calls),repeatContentConnects=0,sameComponentsWrite=True)
            finally:runtime.close()

    def test_unfinished_select_blocks_cas_and_original_cursor_remains_usable(self):
        with self.warm_clean(),self.seed.authority(),ExitStack() as owners:
            writer,_context=self.fixture.admission(owners);runtime=self.fixture.runtime(writer)
            cursor=None
            try:
                mark=self.api();before=self.J.inspect_journal(self.case.journal)
                cursor=runtime.index.db.execute('SELECT value FROM schema_meta ORDER BY key')
                self.assertFalse(runtime.index.db.in_transaction)
                with self.assertRaisesRegex(ValueError,'normal_writer_snapshot_busy'):mark(writer)
                self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'],before['epoch'])
                self.assertIsNotNone(cursor.fetchone());cursor.fetchall();cursor.close();cursor=None
                self.N.validate_normal_writer(writer)
                self.assertTrue(mark(writer)['productRouteAllowed'])
                emit('normal-intent-unfinished-cursor',inTransaction=False,casBeforeDrain=False,originalCursorPreserved=True)
            finally:
                if cursor is not None:cursor.close()
                runtime.close()

    def test_reader_selection_race_with_same_bundle_epoch_refuses_inside_image_cas(self):
        with self.seed.authority():
            reader=self.seed.reader
            alternate=self.seed.helper.alternate(self.case,reader,self.seed.installer,'normal-intent-reader-copy.sqlite')
            selection=reader.read_selection(self.case.journal)
            with self.warm_clean(),ExitStack() as owners:
                writer,_context=self.fixture.admission(owners);runtime=self.fixture.runtime(writer)
                before=self.J.inspect_journal(self.case.journal);injected=[False]
                original=reader.mutate_image
                def race(path,operation):
                    if not injected[0]:
                        injected[0]=True
                        reader.cas_reader(self.case.journal,expected_epoch=selection.epoch,
                            expected_endpoint=selection.preferred_endpoint,target=alternate,transition_id='b'*32)
                    return original(path,operation)
                try:
                    mark=self.api()
                    with mock.patch.object(reader,'mutate_image',side_effect=race):
                        with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):mark(writer)
                    self.assertTrue(injected[0])
                    after=self.J.inspect_journal(self.case.journal)
                    self.assertEqual(after['epoch'],before['epoch'])
                    self.assertEqual(reader.read_selection(self.case.journal).epoch,selection.epoch+1)
                    with self.assertRaises(ValueError):self.N.validate_normal_writer(writer)
                    emit('normal-intent-reader-cas-race',readerEpochChanged=True,bundleMarkCommitted=False,writerRevoked=True)
                finally:runtime.close()

    def test_post_cas_native_close_failure_preserves_mark_but_revokes_writer_until_drain(self):
        with self.warm_clean(),self.seed.authority(),ExitStack() as owners:
            writer,_context=self.fixture.admission(owners);runtime=self.fixture.runtime(writer)
            readers=importlib.import_module('laya_writer_proof_reader')
            before=self.J.inspect_journal(self.case.journal);phase=[0];blocked=[True]
            verify=self.J.verify_selected_route_for_write;native=self.W._OwnedSQLite._native_close_locked
            def proof(*args,**kwargs):phase[0]+=1;return verify(*args,**kwargs)
            def close(db):
                if phase[0]==2 and blocked[0]:raise sqlite3.OperationalError('normal-post-cas-proof-close-failure')
                return native(db)
            try:
                mark=self.api()
                with mock.patch.object(self.J,'verify_selected_route_for_write',side_effect=proof), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):mark(writer)
                    self.assertEqual(phase[0],2)
                    self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'],before['epoch']+1)
                    self.assertTrue(self.J.inspect_journal(self.case.journal)['newWriteSeen'])
                    with self.assertRaises(ValueError):self.N.validate_normal_writer(writer)
                    self.assertGreater(readers.pending_proof_readers(),0)
                    owners.close()
                    with self.sync.sync_owner(self.queue) as busy:self.assertIsNone(busy)
                    blocked[0]=False
                    self.assertEqual(readers.drain_proof_readers(deadline=time.monotonic()+2),0)
                    runtime.close();runtime=None
                with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
                emit('normal-intent-post-cas-close-failure',markPreserved=True,writerRevoked=True,
                     creatorProofDrainThenRuntimeClose=True)
            finally:
                blocked[0]=False;readers.drain_proof_readers(deadline=time.monotonic()+2)
                if runtime is not None:runtime.close()

if __name__=='__main__':unittest.main()
