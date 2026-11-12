#!/usr/bin/env python3
"""Normal first-write epoch refresh and journal-image CAS preconditions."""
from contextlib import ExitStack, closing, contextmanager
import fcntl
import importlib
import importlib.util
import gc
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
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

    def test_worker_mark_validation_timeout_refuses_before_cas(self):
        with self.warm_clean(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            writer = owners.enter_context(self.N.worker_writer_scope(
                self.fixture.plan, queue_owner=owner, operation_timeout=2))
            before = self.J.inspect_journal(self.case.journal)
            clock = [time.monotonic()]
            validate = self.P.validate_writer_plan
            def slow(plan):
                result = validate(plan)
                clock[0] += 3
                return result
            with mock.patch.object(self.N, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                    mock.patch.object(self.P, 'validate_writer_plan', side_effect=slow), \
                    mock.patch.object(self.N.R, 'mutate_image', side_effect=AssertionError('cas reached')) as cas:
                with self.assertRaisesRegex(ValueError, 'normal_writer_deadline'):
                    self.N.mark_new_write(writer)
                cas.assert_not_called()
            self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], before['epoch'])

    def test_worker_marked_repeat_validation_timeout_does_not_return_proof(self):
        with self.warm_clean(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            writer = owners.enter_context(self.N.worker_writer_scope(
                self.fixture.plan, queue_owner=owner, operation_timeout=2))
            first = self.N.mark_new_write(writer)
            clock = [time.monotonic()]
            validate = self.P.validate_writer_plan
            def slow(plan):
                result = validate(plan)
                clock[0] += 3
                return result
            with mock.patch.object(self.N, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                    mock.patch.object(self.P, 'validate_writer_plan', side_effect=slow), \
                    mock.patch.object(self.N.R, 'mutate_image') as cas:
                with self.assertRaisesRegex(ValueError, 'normal_writer_deadline'):
                    self.N.mark_new_write(writer)
                cas.assert_not_called()
            self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'], first['selectionEpoch'])

    def test_worker_mark_timeout_after_replace_revokes_writer(self):
        with self.warm_clean(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            writer = owners.enter_context(self.N.worker_writer_scope(
                self.fixture.plan, queue_owner=owner, operation_timeout=2))
            before = self.J.inspect_journal(self.case.journal)
            clock = [time.monotonic()]
            mutate = self.N.R.mutate_image
            def slow(*args, **kwargs):
                result = mutate(*args, **kwargs)
                clock[0] += 3
                return result
            with mock.patch.object(self.N, 'time', SimpleNamespace(monotonic=lambda: clock[0])), \
                    mock.patch.object(self.N.R, 'mutate_image', side_effect=slow):
                with self.assertRaisesRegex(ValueError, 'normal_writer_deadline'):
                    self.N.mark_new_write(writer)
            after = self.J.inspect_journal(self.case.journal)
            self.assertEqual(after['epoch'], before['epoch'] + 1)
            self.assertTrue(after['newWriteSeen'])
            with self.assertRaises(ValueError):
                self.N.validate_normal_writer(writer)

    @contextmanager
    def held_reader(self, hold, sql=()):
        """A reader holding every physical lock SH for ``hold`` seconds of each
        writer proof, on a clock only the writer modules read (no sleep).
        ``sql[i]`` seconds pass inside proof ``i``'s route proof."""
        proof_reader = importlib.import_module('laya_writer_proof_reader')
        lock_dir = self.N.R._lock_dir(self.fixture.plan.install_id, create=False)
        locks = sorted(lock_dir.glob('*-physical.lock'))
        self.assertTrue(locks)
        offset, waited, held, proofs = [0.0], [0.0], [], []

        def release():
            while held:
                os.close(held.pop())

        def sleep(seconds):
            offset[0] += seconds
            waited[0] += seconds
            if held and waited[0] >= hold:
                release()
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0], sleep=sleep, time=time.time)
        windows, verify = self.P.writer_proof_windows, self.J.verify_selected_route_for_write

        def held_windows(*args, **kwargs):
            proofs.append(len(proofs))
            waited[0] = 0.0
            for path in locks:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_SH)
                held.append(fd)
            return windows(*args, **kwargs)

        def slow_verify(decision):
            index = len(proofs) - 1
            offset[0] += sql[index] if index < len(sql) else 0.0
            return verify(decision)
        try:
            with mock.patch.object(self.N, 'time', clock), mock.patch.object(self.P, 'time', clock), \
                    mock.patch.object(proof_reader, 'time', clock), \
                    mock.patch.object(self.P, 'writer_proof_windows', side_effect=held_windows), \
                    mock.patch.object(self.J, 'verify_selected_route_for_write', side_effect=slow_verify):
                yield proofs
        finally:
            release()

    def test_worker_mark_gives_each_proof_its_own_deadline_under_a_held_reader(self):
        """Two proofs that each wait 1.5 s for a reader fit their own 2 s deadline."""
        with self.warm_clean(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            writer = owners.enter_context(self.N.worker_writer_scope(
                self.fixture.plan, queue_owner=owner, operation_timeout=2))
            before = self.J.inspect_journal(self.case.journal)
            with self.held_reader(1.5) as proofs:
                proof = self.N.mark_new_write(writer)
            self.assertEqual(len(proofs), 2)
            after = self.J.inspect_journal(self.case.journal)
            self.assertEqual((after['epoch'], after['newWriteSeen']), (before['epoch'] + 1, True))
            self.assertEqual(proof['selectionEpoch'], after['epoch'])
            self.N.validate_normal_writer(writer)

    def test_worker_mark_proof_over_its_deadline_reports_stage_timings(self):
        """A proof that waits 1.5 s and then reads for 1 s ends writer_proof_deadline
        with the measured lock and SQL time of every stage."""
        with self.warm_clean(), self.seed.authority(), ExitStack() as owners:
            owner = owners.enter_context(self.sync.sync_owner(self.queue))
            writer = owners.enter_context(self.N.worker_writer_scope(
                self.fixture.plan, queue_owner=owner, operation_timeout=2))
            before = self.J.inspect_journal(self.case.journal)
            with self.held_reader(1.5, sql=(0.0, 1.0)) as proofs:
                with self.assertRaisesRegex(self.W.ReaderWindowBusy, 'writer_proof_deadline') as caught:
                    self.N.mark_new_write(writer)
            self.assertEqual(len(proofs), 2)
            timings = caught.exception.mark_timings
            self.assertEqual(set(timings), {'p1_lock', 'p1_sql', 'img', 'p2_lock', 'p2_sql'})
            self.assertGreaterEqual(timings['p1_lock'], 1500)
            self.assertGreaterEqual(timings['p2_lock'], 1500)
            self.assertGreaterEqual(timings['p2_sql'], 1000)
            # The mark committed before the second proof; the writer is revoked.
            after = self.J.inspect_journal(self.case.journal)
            self.assertEqual(after['epoch'], before['epoch'] + 1)
            with self.assertRaises(ValueError):
                self.N.validate_normal_writer(writer)

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
                def race(path,operation,**kwargs):
                    if not injected[0]:
                        injected[0]=True
                        reader.cas_reader(self.case.journal,expected_epoch=selection.epoch,
                            expected_endpoint=selection.preferred_endpoint,target=alternate,transition_id='b'*32)
                    return original(path,operation,**kwargs)
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

    def test_publication_after_private_image_fsync_rechecks_original_lease_and_deadline(self):
        with self.warm_clean(),self.seed.authority():
            mark=self.api()
            for mode in ('lease','deadline'):
                with self.subTest(mode=mode),ExitStack() as owners:
                    self.fixture.plan=self.P.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
                    writer,_context=self.fixture.admission(owners);runtime=self.fixture.runtime(writer)
                    before=self.case.journal.read_bytes();epoch=self.J.inspect_journal(self.case.journal)['epoch']
                    clock=time.monotonic;advanced=[False];observed=[]
                    def now():return clock()+(100 if advanced[0] else 0)
                    def step(name):
                        if name=='v5-image-fsynced':
                            observed.append(name)
                            if mode=='lease':owners.close()
                            else:advanced[0]=True
                    try:
                        with mock.patch.object(time,'monotonic',side_effect=now), \
                                mock.patch.object(self.J,'_conversion_step',new=step):
                            with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):mark(writer)
                        changed=self.case.journal.read_bytes()!=before
                        emit('normal-intent-expired-at-publication',mode=mode,atFsync=observed,journalChanged=changed)
                        self.assertEqual(observed,['v5-image-fsynced'])
                        self.assertFalse(changed,'expired publication must preserve the original journal image')
                        self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'],epoch)
                        with self.assertRaises(ValueError):self.N.validate_normal_writer(writer)
                    finally:runtime.close()

    def test_callable_native_cursor_factory_is_busy_without_attribute_error_or_cursor_reset(self):
        with self.warm_clean(),self.seed.authority(),ExitStack() as owners:
            writer,_context=self.fixture.admission(owners);runtime=self.fixture.runtime(writer)
            cursor=None
            try:
                mark=self.api();before=self.J.inspect_journal(self.case.journal)['epoch']
                cursor=runtime.index.db.cursor(factory=lambda db:sqlite3.Cursor(db))
                cursor.execute('SELECT value FROM schema_meta ORDER BY key')
                error=None
                try:mark(writer)
                except Exception as exc:error=exc
                emit('normal-intent-callable-cursor',errorType=type(error).__name__,error=str(error))
                self.assertIsInstance(error,ValueError)
                self.assertIn('normal_writer_snapshot_busy',str(error))
                self.assertIsNotNone(cursor.fetchone())
                self.assertEqual(self.J.inspect_journal(self.case.journal)['epoch'],before)
                cursor.close();cursor=None;gc.collect()
                self.N.validate_normal_writer(writer)
                self.assertTrue(mark(writer)['productRouteAllowed'])
            finally:
                if cursor is not None:cursor.close()
                runtime.close()

if __name__=='__main__':unittest.main()
