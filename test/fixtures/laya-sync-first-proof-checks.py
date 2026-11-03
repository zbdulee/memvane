#!/usr/bin/env python3
"""Actual selected worker first proof; later worker opens remain separate."""
from contextlib import ExitStack, closing, contextmanager
from contextvars import copy_context
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('sync_first_seed',ROOT/'test/fixtures/laya-normal-writer-connections-checks.py')
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

HOT_CHILD="""import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1],isolation_level=None)
db.execute('PRAGMA cache_size=1');db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE');db.execute('DROP TABLE selection');db.execute('CREATE TABLE junk(x)')
for _ in range(300):db.execute('INSERT INTO junk VALUES(randomblob(4000))')
os._exit(9)
"""

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

    def publish_v4(self):
        tmp=self.case.journal.with_name('test-legacy-v4-image')
        tmp.write_bytes(self.v4);tmp.chmod(0o600);os.replace(tmp,self.case.journal)

    def crash_hot(self):
        child=subprocess.run([sys.executable,'-B','-I','-c',HOT_CHILD,str(self.case.journal)],
            encoding='utf8',capture_output=True,timeout=10)
        self.assertEqual(child.returncode,9,child.stderr)
        self.assertTrue(Path(str(self.case.journal)+'-journal').exists())
        with closing(sqlite3.connect(self.case.journal.as_uri()+'?mode=ro',uri=True)) as db:
            with self.assertRaises(sqlite3.OperationalError):db.execute('SELECT count(*) FROM selection').fetchone()

    def journal_connects(self,calls):
        connect=sqlite3.connect
        def observe(database,*args,**kwargs):
            if str(database).startswith(self.case.journal.as_uri()+'?mode=rw'):
                calls.append(str(database))
            return connect(database,*args,**kwargs)
        return mock.patch.object(sqlite3,'connect',side_effect=observe)

    @contextmanager
    def markerless(self):
        fresh=importlib.import_module('laya_fresh_wiki_origin')
        ledger,marker,_route=fresh._paths(self.case.project,self.case.journal)
        saved=[]
        for path in (marker,ledger):
            target=path.with_name(path.name+'.saved-first-proof')
            os.rename(path,target);saved.append((path,target))
        try:yield
        finally:
            for path,target in reversed(saved):os.rename(target,path)

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

    def test_legacy_v4_hot_journal_preserves_existing_worker_recovery(self):
        with self.fixture.F.warm(),self.seed.authority():
            self.publish_v4()
            self.assertEqual(self.J.journal_version(self.case.journal),4)
            side=Path(str(self.case.journal)+'-journal')
            # The unchanged public legacy proof supplies the minimum baseline.
            self.crash_hot();decision=self.worker.select_route(str(self.case.project),action='update')
            baseline=self.worker.verify_selected_route(decision)
            self.assertTrue(baseline['productRouteAllowed']);self.assertFalse(side.exists())
            self.crash_hot();error=None;proof=None
            try:_decision,proof=self.selected()
            except Exception as exc:error=type(exc).__name__+':'+str(exc)
            emit('worker-legacy-hot-v4',baselineApproved=True,error=error,selected=proof is not None,hotSidecarRemains=side.exists())
            self.assertIsNone(error)
            self.assertEqual(proof,baseline);self.assertFalse(side.exists())
            self.assertEqual(self.J.journal_version(self.case.journal),4)

    def test_foreign_queue_or_journal_route_is_rejected_before_owner_lock_or_rw(self):
        access=importlib.import_module('laya_worker_journal')
        host=importlib.import_module('laya_index_host_route')
        foreign=self.case.home/'foreign-journal-namespace';foreign.mkdir(mode=0o700)
        route=next(host._candidate_paths(self.case.project))[2];original=route.read_bytes()
        with self.seed.authority():
            for field,source in (('queuePath',self.queue),('journalPath',self.case.journal)):
                target=foreign/source.name
                with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, \
                        closing(sqlite3.connect(target)) as dst:src.backup(dst)
                target.chmod(0o600)
                bad=json.loads(original);bad[field]=str(target)
                route.write_bytes(json.dumps(bad).encode())
                owners=[];rw=[];error=None
                def owner(*args,**kwargs):owners.append(str(args[0]));raise AssertionError('foreign owner acquisition')
                try:
                    with mock.patch.object(access,'sync_owner',side_effect=owner),self.journal_connects(rw):
                        try:self.selected()
                        except Exception as exc:error=str(exc)
                    self.assertIsNotNone(error);self.assertEqual(owners,[]);self.assertEqual(rw,[])
                    self.assertFalse(Path(str(target)+'.sync.lock').exists())
                    emit('worker-foreign-route',field=field,ownerAcquires=owners,rwConnects=rw,error=error)
                finally:route.write_bytes(original)

    def test_copied_expired_legacy_recovery_binding_cannot_open_rw(self):
        access=importlib.import_module('laya_worker_journal')
        with self.seed.authority():
            self.publish_v4();self.crash_hot()
            with access.worker_journal_access(self.case.project),self.J.read_only_journals():copied=copy_context()
            before=self.case.journal.read_bytes();rw=[]
            with self.journal_connects(rw),self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):
                copied.run(self.J.inspect_journal,self.case.journal)
            self.assertEqual(rw,[]);self.assertEqual(self.case.journal.read_bytes(),before)
            self.assertTrue(Path(str(self.case.journal)+'-journal').exists())
            emit('worker-legacy-binding-revoked',rwConnects=rw,originalHotImagePreserved=True)

    def test_v5_header_and_initial_intent_never_receive_worker_rw_recovery(self):
        access=importlib.import_module('laya_worker_journal')
        with self.seed.authority():
            v5=self.case.journal.read_bytes()
            for mode in ('v5','initial-intent'):
                if mode=='initial-intent':
                    # Discard only this test-owned v5 crash image by restoring
                    # its real converter's committed v4 image and journal.
                    with closing(sqlite3.connect(self.case.journal)) as db:db.execute('PRAGMA application_id').fetchone()
                    self.publish_v4()
                self.crash_hot();intent=self.case.journal.parent/self.J._INITIAL_INTENT
                if mode=='initial-intent':intent.write_text('test-owned-initial-intent');intent.chmod(0o600)
                before=self.case.journal.read_bytes();rw=[]
                try:
                    with access.worker_journal_access(self.case.project),self.J.read_only_journals(), \
                            self.journal_connects(rw),self.assertRaises((ValueError,RuntimeError,sqlite3.Error)):
                        self.J.inspect_journal(self.case.journal)
                    self.assertEqual(rw,[]);self.assertEqual(self.case.journal.read_bytes(),before)
                    self.assertTrue(Path(str(self.case.journal)+'-journal').exists())
                    emit('worker-legacy-recovery-refused',mode=mode,rwConnects=rw,hotImagePreserved=True)
                finally:
                    if mode=='initial-intent':intent.unlink()

    def test_markerless_valid_v4_clean_and_hot_preserve_approved_selection(self):
        with self.fixture.F.warm(),self.seed.authority(),self.markerless():
            for mode in ('clean','hot'):
                with self.subTest(mode=mode):
                    self.publish_v4()
                    decision=self.worker.select_route(str(self.case.project),action='update')
                    self.assertEqual(decision['state'],'candidate')
                    baseline=self.worker.verify_selected_route(decision)
                    self.assertTrue(baseline['productRouteAllowed'])
                    if mode=='hot':self.crash_hot()
                    error=None;proof=None
                    try:_decision,proof=self.selected()
                    except Exception as exc:error=type(exc).__name__+':'+str(exc)
                    emit('worker-markerless-v4',mode=mode,baselineApproved=True,error=error,selected=proof is not None)
                    self.assertIsNone(error)
                    self.assertEqual(proof,baseline)
                    self.assertFalse(Path(str(self.case.journal)+'-journal').exists())

    def test_markerless_hot_foreign_route_is_denied_before_queue_owner_creation(self):
        access=importlib.import_module('laya_worker_journal')
        host=importlib.import_module('laya_index_host_route')
        with self.fixture.F.warm(),self.seed.authority(),self.markerless():
            self.publish_v4();self.crash_hot()
            route=next(host._candidate_paths(self.case.project))[2];original=route.read_bytes()
            target=self.case.home/'foreign-queue.sqlite'
            with closing(sqlite3.connect(self.queue)) as source,closing(sqlite3.connect(target)) as copy:source.backup(copy)
            target.chmod(0o600)
            bad=json.loads(original);bad['queuePath']=str(target);route.write_bytes(json.dumps(bad).encode())
            owners=[];rw=[];before=self.case.journal.read_bytes()
            def forbidden(*args,**kwargs):owners.append(True);raise AssertionError('foreign owner created')
            try:
                with mock.patch.object(access,'sync_owner',side_effect=forbidden),self.journal_connects(rw), \
                        self.assertRaises((ValueError,RuntimeError)):
                    self.selected()
                self.assertEqual(owners,[]);self.assertEqual(rw,[])
                self.assertEqual(self.case.journal.read_bytes(),before)
                self.assertFalse(Path(str(target)+'.sync.lock').exists())
                emit('worker-markerless-foreign-hot',ownerAcquires=0,sourceRwConnects=0,hotImagePreserved=True)
            finally:route.write_bytes(original)

    def test_markerless_hot_source_change_at_owner_acquisition_is_denied_without_rw(self):
        access=importlib.import_module('laya_worker_journal')
        with self.fixture.F.warm(),self.seed.authority(),self.markerless():
            self.publish_v4();self.crash_hot()
            owner=access.sync_owner;mutated=[];rw=[]
            def change(queue):
                # This test actor changes the same named image after its
                # private preimage was authenticated, before owner acquisition.
                with self.case.journal.open('ab') as handle:handle.write(b'x')
                mutated.append(self.case.journal.read_bytes())
                return owner(queue)
            with mock.patch.object(access,'sync_owner',side_effect=change),self.journal_connects(rw), \
                    self.assertRaises((ValueError,RuntimeError)):
                self.selected()
            self.assertEqual(len(mutated),1);self.assertEqual(rw,[])
            self.assertEqual(self.case.journal.read_bytes(),mutated[0])
            with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
            emit('worker-markerless-source-change',sameInodeChanged=True,sourceRwConnects=0,ownerReleased=True)

    def test_probe_sigkill_reuses_bounded_owned_scratch_and_preserves_unknown_files(self):
        with self.fixture.F.warm(),self.seed.authority(),self.markerless():
            self.publish_v4();self.crash_hot();before=self.case.journal.read_bytes();scratch=None
            command=[sys.executable,'-B','-I',str(ROOT/'core/laya_legacy_journal_probe.py'),
                     'probe',str(self.case.journal),'--checkpoint-after-copy']
            for _ in range(2):
                child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,encoding='utf8')
                try:
                    self.assertTrue(select.select([child.stdout],[],[],4)[0],'private copy checkpoint deadline')
                    line=child.stdout.readline()
                    self.assertTrue(line,'probe child exited before checkpoint')
                    checkpoint=json.loads(line);self.assertEqual(checkpoint['stage'],'private-copy-ready')
                    current=Path(checkpoint['scratch'])
                    if scratch is not None:self.assertEqual(current,scratch)
                    scratch=current;child.kill();self.assertEqual(child.wait(timeout=2),-signal.SIGKILL)
                finally:
                    if child.poll() is None:child.kill()
                    child.communicate(timeout=2)
                self.assertEqual(self.case.journal.read_bytes(),before)
                self.assertEqual(set(os.listdir(scratch)),{'.owner.json','probe.sqlite','probe.sqlite-journal'})
            unknown=scratch/'user-owned.txt';unknown.write_text('preserve');unknown.chmod(0o600)
            try:
                with self.assertRaises((ValueError,RuntimeError)):self.selected()
                self.assertEqual(unknown.read_text(),'preserve')
                self.assertEqual(self.case.journal.read_bytes(),before)
            finally:unknown.unlink()
            _decision,proof=self.selected()
            self.assertTrue(proof['productRouteAllowed'])
            self.assertEqual(set(os.listdir(scratch)),{'.owner.json'})
            emit('worker-legacy-probe-sigkill',kills=2,sameOwnedScratch=True,
                 unknownPreserved=True,retryRecovered=True)

if __name__=='__main__':unittest.main()
