#!/usr/bin/env python3
"""Normal base/queue managed opens; capacity/sidecar/cohort remain separate."""
from contextlib import ExitStack, closing
import gc
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest import mock
import weakref

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('normal_connection_seed', ROOT/'test/fixtures/laya-writer-scope-checks.py')
existing = importlib.util.module_from_spec(spec); spec.loader.exec_module(existing)

def emit(label, **values): print(json.dumps(dict(case=label, **values), sort_keys=True), flush=True)

class NormalWriterConnectionChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.WriterScopeChecks('runTest')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.F, self.seed, self.case = self.fixture.F, self.fixture.seed, self.fixture.case
        self.P, self.W, self.J = self.fixture.P, self.fixture.W, self.fixture.J
        self.sync, self.queue, self.plan = self.fixture.sync, self.fixture.queue, self.fixture.plan
        self.N = importlib.import_module('laya_writer_scope')

    def component(self, writer, role):
        try: return self.W.component_for(writer, role)
        except (ValueError, RuntimeError) as exc:
            self.fail('normal component feature absent after actual managed fullproof: '+str(exc))

    def admission(self, stack):
        owner = stack.enter_context(self.sync.sync_owner(self.queue))
        context = self.N.normal_writer_scope(self.plan, queue_owner=owner, deadline=time.monotonic()+20)
        writer = stack.enter_context(context)
        return writer, context

    def runtime(self, writer):
        from laya_index_runtime import LayaIndexRuntime
        windows = {role:self.component(writer,role) for role in ('base','queue')}
        return LayaIndexRuntime(source_root={self.case.WIKI:str(self.case.auto/'wiki')},
            scope=self.case.route['scopeId'], db_path=str(self.case.db), queue_path=str(self.queue),
            db_incarnation=self.case.route['dbIncarnation'], wiki_only=True, connection_windows=windows)

    def test_actual_base_queue_runtime_writes_and_pins_outlive_lexical_scope(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, context = self.admission(owners); calls = []; runtime = None
            try:
                with self.fixture.fixture.trace(calls): runtime = self.runtime(writer)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(c['managed'] for c in calls))
                self.assertEqual(runtime.index.exclude(writer.scope_id,path='normal-writer-exclude.md'),'excluded')
                owners.close(); gc.collect()
                with self.assertRaises(ValueError): self.N.validate_normal_writer(writer)
                with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                runtime.close(); runtime = None
                with self.sync.sync_owner(self.queue) as successor: self.assertIsNotNone(successor)
                emit('normal-runtime-base-queue', contentConnects=calls, actualWrite=True,
                     pinOutlivesLexical=True, successorAfterNativeClose=True)
            finally:
                if runtime is not None: runtime.close()

    def test_same_inode_valid_foreign_queue_binding_is_rejected_before_rw_connect(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); before = self.queue.stat()
            from laya_index_claim_queue import ClaimQueue
            with sqlite3.connect(self.queue) as db:
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='queue_meta_update_seal'").fetchone()[0]
                db.execute('DROP TRIGGER queue_meta_update_seal')
                db.execute("UPDATE queue_meta SET value=? WHERE key='index_incarnation'",('e'*32,))
                db.execute(trigger); db.commit()
                self.assertEqual(ClaimQueue._check_database(db),'e'*32)
            self.assertEqual((self.queue.stat().st_dev,self.queue.stat().st_ino),(before.st_dev,before.st_ino))
            calls = []; error = None
            with self.fixture.fixture.trace(calls):
                try: ClaimQueue.open(self.queue, coordination=component)
                except (ValueError, RuntimeError, sqlite3.Error) as exc: error = str(exc)
            emit('normal-queue-same-inode-foreign-binding', contentConnects=calls,error=error)
            self.assertIsNotNone(error)
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['uri'].endswith('?mode=ro'))

    def test_scope_exit_after_ro_close_prevents_rw_connect(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, context = self.admission(owners)
            component = self.component(writer,'queue'); calls = []; closes = []
            from laya_index_claim_queue import ClaimQueue
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                result = native(db); closes.append(True)
                if len(closes)==1: context.__exit__(None,None,None)
                return result
            error = None
            with self.fixture.fixture.trace(calls), mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                try: ClaimQueue.open(self.queue,coordination=component)
                except (ValueError,RuntimeError,sqlite3.Error) as exc: error = str(exc)
            self.assertIsNotNone(error)
            self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['uri'].endswith('?mode=ro'))
            emit('normal-expired-between-ro-rw', contentConnects=calls,error=error)

    def test_auxiliary_loss_after_admission_refuses_before_any_content_connect(self):
        with self.seed.authority(), ExitStack() as owners:
            warm = self.F.warm(); warm.__enter__()
            try:
                writer, _context = self.admission(owners)
                component = self.component(writer,'base')
            finally: warm.__exit__(None,None,None)
            self.assertFalse(Path(str(self.case.db)+'-wal').exists())
            self.assertFalse(Path(str(self.case.db)+'-shm').exists())
            from laya_index_a import LayaIndexA
            files = existing.existing.existing.files
            before = files(self.case.db); calls = []
            with self.fixture.fixture.trace(calls), self.assertRaises((ValueError,RuntimeError)):
                LayaIndexA(self.case.db,coordination=component)
            self.assertEqual(calls,[]); self.assertEqual(files(self.case.db),before)
            emit('normal-post-admission-cold-refusal', contentConnects=calls,newAux=0)

    def test_first_rw_pragma_and_native_close_failure_keep_pin_until_creator_drain(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'base')
            from laya_index_a import LayaIndexA
            actual = sqlite3.connect; execute = self.W._OwnedSQLite.execute
            native = self.W._OwnedSQLite._native_close_locked
            refs = []; blocked = [True]; close_events = []
            def connect(database,*args,**kwargs):
                db = actual(database,*args,**kwargs)
                if 'mode=rw' in str(database) and isinstance(db,self.W._OwnedSQLite):
                    db._normal_test_rw = True; refs.append(weakref.ref(db))
                return db
            def sql(db,statement,*args,**kwargs):
                if getattr(db,'_normal_test_rw',False) and statement=='PRAGMA wal_autocheckpoint=0':
                    raise sqlite3.OperationalError('normal-first-rw-pragma-failure')
                return execute(db,statement,*args,**kwargs)
            def close(db):
                if getattr(db,'_normal_test_rw',False):
                    if blocked[0]: raise sqlite3.OperationalError('normal-native-close-failure')
                    close_events.append(not self.F.fixture.probe('base-physical.lock','SH'))
                return native(db)
            try:
                with mock.patch.object(sqlite3,'connect',side_effect=connect), \
                        mock.patch.object(self.W._OwnedSQLite,'execute',new=sql), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):
                        LayaIndexA(self.case.db,coordination=component)
                    owners.close(); gc.collect()
                    self.assertEqual(len(refs),1); self.assertIsNotNone(refs[0]())
                    self.assertFalse(refs[0]()._native_closed)
                    self.assertEqual(self.W.pending_count(),1)
                    with self.sync.sync_owner(self.queue) as busy: self.assertIsNone(busy)
                    other=[]
                    thread=threading.Thread(target=lambda:other.append(self.W.drain_pending(deadline=time.monotonic()+1)))
                    thread.start();thread.join(3)
                    self.assertFalse(thread.is_alive());self.assertEqual(other,[1])
                    blocked[0]=False
                    self.assertEqual(self.W.drain_pending(deadline=time.monotonic()+2),0)
                self.assertEqual(close_events,[True])
                with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
                emit('normal-failed-init-close-owner', weakNativeAlive=True,otherThread=other,
                     nativeCloseUnderEX=close_events, successorAfterNativeDrain=True)
            finally:
                blocked[0]=False;self.W.drain_pending(deadline=time.monotonic()+2)

    def test_normal_authority_cannot_prepare_or_repair_window_namespace(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            self.component(writer,'base')
            parent = self.F.fixture.parent
            snapshot = self.F.fixture.fixture.snapshot
            before = snapshot(parent)
            with self.assertRaises(ValueError): self.W.prepare_windows(writer)
            self.assertEqual(snapshot(parent),before)

    def test_preferred_reader_copy_does_not_redirect_normal_base_component(self):
        with self.seed.authority():
            reader = self.seed.reader
            alternate = self.seed.helper.alternate(self.case,reader,self.seed.installer,'normal-reader-copy.sqlite')
            current = reader.read_selection(self.case.journal)
            reader.cas_reader(self.case.journal,expected_epoch=current.epoch,
                expected_endpoint=current.preferred_endpoint,target=alternate,transition_id='c'*32)
            self.plan = self.P.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
            with self.F.warm(), ExitStack() as owners:
                writer, _context = self.admission(owners)
                component = self.component(writer,'base')
                self.assertEqual(component.path,self.case.db)
                self.assertNotEqual(component.path,Path(alternate.record['db_path']))
                from laya_index_a import LayaIndexA
                with closing(LayaIndexA(self.case.db,coordination=component)) as index:
                    self.assertEqual(index.incarnation(),writer.incarnation)
                emit('normal-canonical-writer-versus-reader',writerBase=str(component.path),
                     readerBase=alternate.record['db_path'])

    def test_queue_content_change_after_ro_close_cannot_reach_rw_open(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners, \
                closing(sqlite3.connect(self.queue)) as mutator:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); calls = []; closes = []
            from laya_index_claim_queue import ClaimQueue
            native = self.W._OwnedSQLite._native_close_locked
            def close(db):
                result = native(db); closes.append(True)
                if len(closes)==1:
                    trigger = mutator.execute("SELECT sql FROM sqlite_master WHERE name='queue_meta_update_seal'").fetchone()[0]
                    mutator.execute('DROP TRIGGER queue_meta_update_seal')
                    mutator.execute("UPDATE queue_meta SET value=? WHERE key='index_incarnation'",('f'*32,))
                    mutator.execute(trigger); mutator.commit()
                    self.assertEqual(ClaimQueue._check_database(mutator),'f'*32)
                return result
            error = None; queue = None
            try:
                with self.fixture.fixture.trace(calls), mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    try: queue = ClaimQueue.open(self.queue,coordination=component)
                    except (ValueError,RuntimeError,sqlite3.Error) as exc: error = str(exc)
                emit('normal-content-change-after-ro-close',contentConnects=calls,error=error,returned=queue is not None)
                self.assertIsNotNone(error,'changed queue identity must not return a normal writer')
                self.assertEqual(len(calls),1,'committed content change after RO close must prevent RW open')
            finally:
                if queue is not None: queue.close()

    def test_deadline_crossed_inside_actual_initializer_cannot_return_rw_handle(self):
        with self.F.warm(), self.seed.authority(), ExitStack() as owners:
            writer, _context = self.admission(owners)
            component = self.component(writer,'queue'); advanced = [False]
            from laya_index_claim_queue import ClaimQueue
            initialize = ClaimQueue._initialize_connection; clock = time.monotonic
            def now(): return clock()+(100 if advanced[0] else 0)
            def init(queue,db):
                result=initialize(queue,db); advanced[0]=True; return result
            error = None; queue = None
            try:
                with mock.patch.object(time,'monotonic',side_effect=now), \
                        mock.patch.object(ClaimQueue,'_initialize_connection',new=init):
                    try: queue=ClaimQueue.open(self.queue,coordination=component)
                    except (ValueError,RuntimeError,sqlite3.Error) as exc: error=str(exc)
                emit('normal-expired-inside-initializer',actualInitializerCompleted=advanced[0],
                     returned=queue is not None,error=error)
                self.assertTrue(advanced[0]);self.assertIsNotNone(error)
                self.assertIsNone(queue,'expired initializer must not return a normal writer handle')
                self.assertEqual(self.W.pending_count(),0)
            finally:
                if queue is not None:queue.close()

if __name__=='__main__': unittest.main()
