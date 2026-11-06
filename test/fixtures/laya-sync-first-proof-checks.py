#!/usr/bin/env python3
"""Actual selected worker first proof; later worker opens remain separate."""
from contextlib import ExitStack, closing, contextmanager, nullcontext
from contextvars import copy_context
import importlib
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
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

NORMAL_CREATE_CRASH = r'''
import importlib.util,json,os,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
root,payload=Path(sys.argv[1]),Path(sys.argv[2]); p=json.loads(payload.read_text())
sys.path.insert(0,str(root/'core'));os.environ['HOME']=p['home'];os.environ['MEMVANE_TEST_RUNTIME_ROOT']=p['runtime']
signal.alarm(35)
spec=importlib.util.spec_from_file_location('normal_create_base',root/'test/fixtures/laya-journal-reader-checks.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
import laya_writer_open_plan as P,laya_writer_scope as N,laya_reader_windows as W,laya_sidecar_create as C
from laya_index_sync import sync_owner
helper=base.JournalReaderChecks('runTest');case=SimpleNamespace(plugin=Path(p['plugin']),proof=p['proof'])
actual=C._rename_noreplace
def crash(parent,src,dst):
    st=os.stat(src,dir_fd=parent,follow_symlinks=False)
    evidence={'event':'rename-checkpoint','mode':p['mode'],'source':src,
              'node':[st.st_dev,st.st_ino],'pid':os.getpid()}
    if p['mode']=='before':
        print(json.dumps(evidence),flush=True)
        os.kill(os.getpid(),signal.SIGKILL)
    actual(parent,src,dst)
    print(json.dumps(evidence),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
with helper.full_authority(case):
    plan=P.capture_writer_plan(p['project'],executing_root=p['plugin'])
    with sync_owner(Path(plan.queue_path)) as owner:
        with N.worker_writer_scope(plan,queue_owner=owner,operation_timeout=10) as writer:
            N.mark_new_write(writer)
            component=W.component_for(writer,'sidecar')
            with mock.patch.object(C,'_rename_noreplace',new=crash):
                W.create_sidecar_owned(component,deadline=time.monotonic()+10)
raise RuntimeError('rename checkpoint not reached')
'''

def emit(label,**values):print(json.dumps(dict(case=label,**values),sort_keys=True),flush=True)

class SyncFirstProofChecks(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.NormalWriterConnectionChecks('runTest')
        self.addCleanup(self.fixture.doCleanups)
        reader=importlib.import_module('laya_journal_reader')
        convert=reader.convert_reader_v5;self.v4=None
        def observed(state,*args,**kwargs):
            absent_sidecar_cases = {
                'test_capacity_worker_creates_absent_sidecar_after_fence',
                'test_capacity_worker_refuses_to_recreate_lost_required_sidecar',
                'test_capacity_worker_resumes_empty_cold_sidecar_after_fence',
                'test_capacity_worker_rejects_nonempty_cold_delete_sidecar',
                'test_capacity_worker_resumes_after_interrupted_sidecar_rename',
                'test_capacity_worker_drains_failed_sidecar_material_close',
                'test_capacity_worker_recovers_fresh_sigkill_before_sidecar_rename',
                'test_capacity_worker_recovers_fresh_sigkill_after_sidecar_rename',
                'test_unmarked_writer_cannot_create_sidecar',
                'test_capacity_worker_defers_writer_deadline',
                'test_capacity_worker_propagates_pending_rollback',
                'test_capacity_worker_defers_physical_window_busy',
                'test_capacity_worker_propagates_physical_busy_pending_rollback',
            }
            if self._testMethodName in absent_sidecar_cases:
                import laya_incremental_docs
                sidecar = Path(state) / laya_incremental_docs.FILE
                for suffix in ('', '-wal', '-shm', '-journal'):
                    Path(str(sidecar) + suffix).unlink(missing_ok=True)
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

    def test_prewarm_reproof_uses_owned_readers_before_provider_start(self):
        """PR5: the second actual worker proof must not reopen content raw."""
        class ReachedPrewarm(Exception):
            pass
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            calls = []
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value='synthetic'), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch('laya_embed_resident.ensure_running', side_effect=ReachedPrewarm):
                with self.assertRaises(ReachedPrewarm):
                    self.worker.run(self.case.project)
            emit('worker-prewarm-reproof', contentConnects=calls)
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call['managed'] for call in calls), calls)

    def test_prewarm_changed_route_refused_before_old_queue_owner(self):
        with self.fixture.F.warm(), self.seed.authority():
            decision, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            changed = dict(decision, queuePath=decision['queuePath'] + '.changed')
            with mock.patch.object(self.worker, '_selected', return_value=(decision, proof)), \
                    mock.patch.object(self.worker, 'select_route', return_value=changed), \
                    mock.patch.object(self.worker, '_route_source', return_value='synthetic'), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, 'sync_owner') as owner, \
                    mock.patch('laya_embed_resident.ensure_running') as ensure:
                with self.assertRaisesRegex(Exception, 'selected_provider_proof_changed'):
                    self.worker.run(self.case.project)
                owner.assert_not_called()
                ensure.assert_not_called()

    def test_worker_attestation_reproof_releases_windows_during_hash(self):
        """PR5: actual full proofs bracket closure work without holding EX."""
        with self.fixture.F.warm(), self.seed.authority():
            decision, proof = self.selected()
            calls, hash_windows = [], []
            def runtime_hash(_record):
                hash_windows.append(self.fixture.F.fixture.probe('base-physical.lock', 'SH'))
            with self.sync.sync_owner(self.queue) as owner, self.trace(calls), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files', side_effect=runtime_hash):
                manifest = self.worker._owned_worker_attestation(decision, owner, proof)
            self.assertEqual(manifest, proof['providerManifest'])
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            self.assertEqual(hash_windows, [True])

    def test_worker_attestation_rejects_changed_second_proof(self):
        with self.fixture.F.warm(), self.seed.authority():
            decision, proof = self.selected()
            epochs = []
            def changed(_record):
                result = self.J.cas_mark_new_write(self.case.journal, proof['selectionEpoch'])
                epochs.append(result['epoch'])
            with self.sync.sync_owner(self.queue) as owner, \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files', side_effect=changed):
                with self.assertRaisesRegex(Exception, 'worker_selection_changed'):
                    self.worker._owned_worker_attestation(decision, owner, proof)
            self.assertEqual(epochs, [proof['selectionEpoch'] + 1])

    def test_worker_attestation_refuses_stale_expected_proof_before_hash(self):
        with self.fixture.F.warm(), self.seed.authority():
            decision, proof = self.selected()
            stale = dict(proof, selectionEpoch=proof['selectionEpoch'] + 1)
            with self.sync.sync_owner(self.queue) as owner, \
                    mock.patch.object(self.J, '_verify_runtime_files') as runtime_hash:
                with self.assertRaisesRegex(Exception, 'worker_selection_changed'):
                    self.worker._owned_worker_attestation(decision, owner, stale)
                runtime_hash.assert_not_called()

    def test_full_worker_post_service_proof_is_owned_before_runtime(self):
        class ReachedRuntime(Exception):
            pass
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            calls = []
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value='synthetic'), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, 'LayaIndexRuntime', side_effect=ReachedRuntime), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                with self.assertRaises(ReachedRuntime):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            self.assertEqual(len(calls), 10)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            service.stop.assert_called_once_with()

    def test_full_worker_changed_after_service_stops_before_runtime(self):
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            def start(*_args):
                self.J.cas_mark_new_write(self.case.journal, proof['selectionEpoch'])
                return service
            with mock.patch.object(self.worker, '_route_source', return_value='synthetic'), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', side_effect=start), \
                    mock.patch.object(self.worker, 'LayaIndexRuntime') as runtime, \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                with self.assertRaisesRegex(Exception, 'selected_proof_changed'):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            runtime.assert_not_called()
            service.stop.assert_called_once_with()

    def test_full_worker_actual_runtime_opens_base_queue_with_writer_ownership(self):
        """Actual worker construction must not bypass its writer lifetime."""
        class ReachedBatch(Exception):
            pass
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            calls = []
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value=str(self.case.auto/'wiki')), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, 'prepare_candidate_batch_off_hook', side_effect=ReachedBatch), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                with self.assertRaises(ReachedBatch):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            self.assertGreaterEqual(len(calls), 12)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            service.stop.assert_called_once_with()

    def test_full_worker_marks_actual_journal_before_publish_with_owned_runtime(self):
        import laya_external_edits
        import laya_writer_proof_reader
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            calls, epochs = [], []
            def publish(_runtime, *, before_publish, **_kwargs):
                before_publish()
                epochs.append(self.J.inspect_journal(self.case.journal)['epoch'])
                return {'state': 'unchanged'}
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value=str(self.case.auto/'wiki')), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, 'prepare_candidate_batch_off_hook', return_value={}), \
                    mock.patch.object(self.worker, '_higher_priority_wake', return_value=False), \
                    mock.patch.object(self.worker, 'sync_full', side_effect=publish), \
                    mock.patch.object(laya_external_edits, 'drain', return_value={}), \
                    mock.patch.object(self.W, 'pending_count', return_value=1), \
                    mock.patch.object(laya_writer_proof_reader, 'pending_proof_readers', return_value=1), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                result = self.worker.run(self.case.project, coordinator=controller,
                                         report={}, start_service=lambda *_a, **_kw: None)
            self.assertEqual(result, {'state': 'unchanged'})
            self.assertEqual(epochs, [proof['selectionEpoch'] + 1])
            self.assertTrue(all(call['managed'] for call in calls), calls)
            service.stop.assert_called_once_with()

    def test_full_worker_actual_changed_card_is_fenced_published_and_acked(self):
        import laya_external_edits
        from laya_index_claim_queue import ClaimQueue
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            old_head=self.case.head()[0]
            profile = proof['vectorProfile']
            provider = SimpleNamespace(
                manifest={'recipeId': 'qmd-regex-token-chunks-candidate-v1'},
                profile={'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                         'embedding_fingerprint': profile['embeddingFingerprint'],
                         'dimension': profile['dimension']},
                embed_document_chunks=lambda _body, path, **_kw: [
                    {'seq': 0, 'pos': 0, 'vector': [1.0] + [0.0] * (profile['dimension'] - 1)}])
            (self.case.auto/'wiki'/'worker-new-card.md').write_text('# 새 카드\n\n검증용 내용\n')
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            original = ClaimQueue.enqueue_next
            fenced = []
            def enqueue(queue, *args, **kwargs):
                fenced.append(self.J.inspect_journal(self.case.journal)['newWriteSeen'])
                return original(queue, *args, **kwargs)
            with mock.patch.object(self.worker, '_route_source', return_value=str(self.case.auto/'wiki')), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, '_higher_priority_wake', return_value=False), \
                    mock.patch.object(ClaimQueue, 'enqueue_next', new=enqueue), \
                    mock.patch.object(laya_external_edits, 'drain', return_value={}), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                result = self.worker.run(self.case.project, coordinator=controller,
                                         report={}, start_service=lambda *_a, **_kw: None)
            self.assertEqual(result['state'], 'indexed', result)
            self.assertEqual(fenced, [True])
            # The revision belongs to the same committed head transition; a
            # queue ACK or journal epoch alone cannot prove this publication.
            with sqlite3.connect(self.case.db.as_uri()+'?mode=ro', uri=True) as db:
                revisions=db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'reader-revision:base:%' ORDER BY key").fetchall()
                head=db.execute('SELECT gen FROM head WHERE scope=?',(self.case.route['scopeId'],)).fetchone()[0]
                baseline_row=db.execute("SELECT value FROM schema_meta WHERE key='reader-revision:base:00000000000000000000'").fetchone()
            self.assertIsNotNone(baseline_row,'worker must seal preexisting head before first visible mutation')
            baseline=json.loads(baseline_row[0]);self.assertEqual(baseline['kind'],'baseline')
            self.assertEqual(baseline['head'],old_head)
            self.assertTrue(revisions,'visible worker publication must durably record a base revision')
            receipt=json.loads(revisions[-1][1])
            self.assertEqual(receipt['component'],'base')
            self.assertRegex(receipt['identity'],r'^[0-9a-f]{64}$')
            expected_identity=hashlib.sha256(json.dumps(['base',self.seed.install,self.case.route['scopeId'],
                self.case.route['dbIncarnation']],ensure_ascii=True,separators=(',',':')).encode('ascii')).hexdigest()
            self.assertEqual(receipt['identity'],expected_identity)
            self.assertEqual(receipt['after'],head)
            self.assertEqual(head,result['generation'])
            with closing(ClaimQueue.open(self.queue)) as queue:
                row = queue.get(result['claim'])
            self.assertEqual(row['state'], 'acked')
            service.stop.assert_called_once_with()

    def test_full_worker_close_failure_drains_before_next_owner(self):
        class ReachedBatch(Exception):
            pass
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            native = self.W._OwnedSQLite._native_close_locked
            state = {'armed': False, 'failed': False, 'failed_id': None, 'closed': []}
            attempts = []
            self.addCleanup(lambda: self.W.drain_pending(deadline=time.monotonic()+2))
            def batch(*_args):
                state['armed'] = True
                raise ReachedBatch()
            def close(db):
                if state['armed']:
                    with self.sync.sync_owner(self.queue) as other:
                        attempts.append(other is None)
                    if not state['failed']:
                        state['failed'] = True
                        state['failed_id'] = id(db)
                        raise sqlite3.OperationalError('injected_runtime_close_failure')
                result = native(db)
                if state['armed']:
                    state['closed'].append(id(db))
                return result
            with mock.patch.object(self.worker, '_route_source', return_value=str(self.case.auto/'wiki')), \
                    mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, 'prepare_candidate_batch_off_hook', side_effect=batch), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'), \
                    mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'injected_runtime_close_failure'):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            self.assertTrue(state['failed'])
            self.assertIn(state['failed_id'], state['closed'])
            self.assertGreaterEqual(len(attempts), 2)
            self.assertTrue(all(attempts))
            self.assertEqual(self.W.local_pending_count(), 0)
            with self.sync.sync_owner(self.queue) as successor:
                self.assertIsNotNone(successor)
            service.stop.assert_called_once_with()

    def test_full_worker_unresolved_close_retains_pin_until_creator_drains(self):
        class ReachedBatch(Exception):
            pass
        with self.fixture.F.warm(), self.seed.authority():
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            service = mock.Mock()
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            native = self.W._OwnedSQLite._native_close_locked
            state = {'armed': False, 'blocked': True}
            def batch(*_args):
                state['armed'] = True
                raise ReachedBatch()
            def close(db):
                if state['armed'] and state['blocked']:
                    raise sqlite3.OperationalError('injected_persistent_close_failure')
                return native(db)
            try:
                with mock.patch.object(self.worker, '_route_source', return_value=str(self.case.auto/'wiki')), \
                        mock.patch.object(self.worker, '_capacity_paths', return_value=None), \
                        mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                        mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                        mock.patch.object(self.worker, '_selected_service', return_value=service), \
                        mock.patch.object(self.worker, 'prepare_candidate_batch_off_hook', side_effect=batch), \
                        mock.patch.object(self.J, 'validate_production_provider_attestation',
                                          return_value=proof['providerManifest']), \
                        mock.patch.object(self.J, '_verify_runtime_files'), \
                        mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'injected_persistent_close_failure'):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                    self.assertGreater(self.W.local_pending_count(), 0)
                    with self.sync.sync_owner(self.queue) as other:
                        self.assertIsNone(other)
                    state['blocked'] = False
                    self.W.drain_pending(deadline=time.monotonic()+2)
                    self.assertEqual(self.W.local_pending_count(), 0)
                    with self.sync.sync_owner(self.queue) as successor:
                        self.assertIsNotNone(successor)
                service.stop.assert_called_once_with()
            finally:
                state['blocked'] = False
                self.W.drain_pending(deadline=time.monotonic()+2)

    def test_background_managed_open_refuses_before_sqlite_and_worker_runs(self):
        from laya_index_a import LayaIndexA
        ready=threading.Event(); release=threading.Event(); errors=[]; opens=[]; observe=[False]
        actual=sqlite3.connect
        def connect(database,*args,**kwargs):
            if observe[0] and threading.current_thread() is not threading.main_thread() and str(database).startswith(self.case.db.as_uri()):
                opens.append(str(database))
            return actual(database,*args,**kwargs)
        def foreign_thread():
            try:
                with self.seed.authority(),ExitStack() as locks:
                    verify=self.seed.mover._locks(locks,self.case.project,self.case.state,
                        self.seed.mover._queue_path(self.case.project,None))
                    verify();self.W.prepare_windows(self.seed.installer)
                    component=self.W.component_for(self.seed.installer,'base')
                    observe[0]=True
                    try:LayaIndexA(self.case.db,coordination=component)
                    except ValueError as exc:
                        if str(exc)!='reader_connection_main_thread_required':raise
                    else:raise AssertionError('background managed open must refuse')
                    finally:observe[0]=False
                    if self.W.local_pending_count()!=0:
                        raise AssertionError('refused open must not leave a pending handle')
                ready.set()
                if not release.wait(20):raise RuntimeError('main worker did not finish')
            except BaseException as exc:
                errors.append(repr(exc));ready.set()
        with ExitStack() as warm,self.seed.authority(),mock.patch.object(sqlite3,'connect',side_effect=connect):
            for path in (self.case.db,self.case.queue,self.case.catalog,self.seed.sidecar):
                db=warm.enter_context(closing(sqlite3.connect(path,isolation_level=None)))
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            decision,proof=self.selected()
            thread=threading.Thread(target=foreign_thread)
            thread.start()
            try:
                self.assertTrue(ready.wait(15),'background attempt must finish')
                self.assertEqual(errors,[])
                self.assertEqual(opens,[])
                self.assertEqual(self.W.pending_count(),0)
                with self.sync.sync_owner(self.queue) as owner:
                    self.assertIsNotNone(owner)
                    with self.worker._full_worker_runtime(decision,owner,proof,
                            str(self.case.auto/'wiki')) as (runtime,writer):
                        self.assertIsNotNone(writer)
                        self.assertIsNotNone(runtime.index.db.execute('SELECT gen FROM head LIMIT 1').fetchone())
            finally:
                release.set();thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(self.W.pending_count(),0)

    def test_capacity_worker_sidecar_close_failure_retains_owner_until_creator_drain(self):
        import laya_incremental_docs
        import laya_wiki_discovery
        from laya_source_catalog import LayaSourceCatalog
        class ReachedCapacity(Exception):
            pass
        roots={self.case.WIKI:str(self.case.auto/'wiki')}
        with self.fixture.F.warm(),self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog,roots,self.case.route['scopeId']):
            _,proof=self.selected();profile=proof['vectorProfile']
            provider=SimpleNamespace(manifest={},profile={'profile_id':profile['profileId'],
                'model_id':profile['modelId'],'embedding_fingerprint':profile['embeddingFingerprint'],
                'dimension':profile['dimension']})
            controller=SimpleNamespace(activity=lambda *_args:nullcontext())
            state={'armed':False,'blocked':True,'failed':0};published=[]
            native=self.W._OwnedSQLite._native_close_locked
            original_init=laya_incremental_docs.PublishedReader.__init__
            def init(instance,*args,**kwargs):
                original_init(instance,*args,**kwargs);published.append(instance)
            def capacity(*_args,**_kwargs):
                self.assertEqual(len(published),1)
                self.assertIsNotNone(published[0]._connect())
                self.assertEqual(published[0]._owner.component.role,'sidecar')
                state['armed']=True
                raise ReachedCapacity()
            def close(db):
                reference=getattr(db,'_window_owner',None)
                owner=reference() if reference is not None else None
                if (state['armed'] and state['blocked'] and owner is not None
                        and owner.component.role=='sidecar'):
                    state['failed']+=1
                    raise sqlite3.OperationalError('injected_capacity_sidecar_close_failure')
                return native(db)
            try:
                with mock.patch.object(self.worker,'_route_source',return_value=roots), \
                        mock.patch.object(self.worker,'_capacity_paths',return_value=(str(self.case.catalog),str(self.case.store))), \
                        mock.patch.object(self.worker,'qa_sync_barrier',return_value=None), \
                        mock.patch.object(self.worker,'ReviewedLocalQmdProvider',return_value=provider), \
                        mock.patch.object(laya_wiki_discovery,'observe_grants',return_value=None), \
                        mock.patch.object(self.worker,'_member_diagnosis',return_value=None), \
                        mock.patch.object(laya_incremental_docs.PublishedReader,'__init__',new=init), \
                        mock.patch.object(self.worker,'_sync_capacity',side_effect=capacity), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected_capacity_sidecar_close_failure'):
                        self.worker.run(self.case.project,coordinator=controller,report={},
                                        start_service=lambda *_a,**_kw:None)
                    self.assertGreater(state['failed'],0)
                    self.assertGreater(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNone(successor)
                    state['blocked']=False
                    self.W.drain_pending(deadline=time.monotonic()+3)
                    self.assertEqual(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
            finally:
                state['blocked']=False
                self.W.drain_pending(deadline=time.monotonic()+3)

    def test_capacity_worker_opens_existing_runtime_with_owned_components(self):
        import laya_incremental_docs
        import laya_wiki_discovery
        from laya_source_catalog import LayaSourceCatalog
        class ReachedCapacity(Exception):
            pass
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        with self.fixture.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']):
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            calls = []
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value=roots), \
                    mock.patch.object(self.worker, '_capacity_paths',
                                      return_value=(str(self.case.catalog), str(self.case.store))), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(laya_wiki_discovery, 'observe_grants', return_value=None), \
                    mock.patch.object(laya_incremental_docs, 'PublishedReader'), \
                    mock.patch.object(self.worker, '_member_diagnosis', return_value=None), \
                    mock.patch.object(self.worker, '_sync_capacity', side_effect=ReachedCapacity):
                with self.assertRaises(ReachedCapacity):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            self.assertGreaterEqual(len(calls), 8)
            self.assertTrue(all(call['managed'] for call in calls), calls)

    def test_capacity_worker_opens_populated_cold_wal_sidecar(self):
        import laya_wiki_discovery
        from laya_source_catalog import LayaSourceCatalog
        class ReachedCapacity(Exception): pass
        roots={self.case.WIKI:str(self.case.auto/'wiki')}
        with self.fixture.F.warm(),self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog,roots,self.case.route['scopeId']):
            with closing(sqlite3.connect(self.seed.sidecar,isolation_level=None)) as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone(),('wal',))
                before=db.execute('SELECT path,hash FROM docs ORDER BY path').fetchall()
            self.assertTrue(before)
            self.assertFalse(any(Path(str(self.seed.sidecar)+suffix).exists() for suffix in ('-wal','-shm')))
            _,proof=self.selected();profile=proof['vectorProfile']
            provider=SimpleNamespace(manifest={},profile={'profile_id':profile['profileId'],
                'model_id':profile['modelId'],'embedding_fingerprint':profile['embeddingFingerprint'],
                'dimension':profile['dimension']})
            controller=SimpleNamespace(activity=lambda *_args:nullcontext())
            with mock.patch.object(self.worker,'_route_source',return_value=roots), \
                    mock.patch.object(self.worker,'_capacity_paths',return_value=(str(self.case.catalog),str(self.case.store))), \
                    mock.patch.object(self.worker,'qa_sync_barrier',return_value=None), \
                    mock.patch.object(self.worker,'ReviewedLocalQmdProvider',return_value=provider), \
                    mock.patch.object(laya_wiki_discovery,'observe_grants',return_value=None), \
                    mock.patch.object(self.worker,'_member_diagnosis',return_value=None), \
                    mock.patch.object(self.worker,'_sync_capacity',side_effect=ReachedCapacity):
                with self.assertRaises(ReachedCapacity):
                    self.worker.run(self.case.project,coordinator=controller,report={},start_service=lambda *_a,**_kw:None)
            with closing(sqlite3.connect(self.seed.sidecar.as_uri()+'?mode=ro',uri=True)) as db:
                self.assertEqual(db.execute('SELECT path,hash FROM docs ORDER BY path').fetchall(),before)

    def test_capacity_worker_existing_sidecar_fences_before_catalog_scan(self):
        import laya_external_edits
        import laya_incremental_docs
        import laya_wiki_discovery
        from laya_index_runtime import LayaIndexRuntime
        from laya_source_catalog import LayaSourceCatalog
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        # Reproduce a real pre-revision sidecar without touching its data.
        with closing(sqlite3.connect(self.seed.sidecar, isolation_level=None)) as legacy:
            legacy.execute('BEGIN IMMEDIATE')
            legacy.execute('DROP TRIGGER publication_revision_insert_seal')
            legacy.execute('DROP TRIGGER publication_revision_update_seal')
            legacy.execute('DROP TRIGGER publication_revision_delete_seal')
            legacy.execute('DROP TABLE publication_revisions')
            legacy.commit()
        with self.fixture.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']), \
                ExitStack() as side_stack, \
                closing(sqlite3.connect(self.case.db.as_uri()+'?mode=ro', uri=True)) as witness:
            self.assertFalse(any(Path(str(self.seed.sidecar)+suffix).exists() for suffix in ('-wal','-shm')),
                             'the worker must be the first cold sidecar opener')
            side_holder=[]
            witness_connect=sqlite3.connect
            def side_db():
                if not side_holder:
                    side_holder.append(side_stack.enter_context(closing(witness_connect(
                        self.seed.sidecar.as_uri()+'?mode=ro',uri=True,isolation_level=None))))
                return side_holder[0]
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(
                manifest={'recipeId': 'qmd-regex-token-chunks-candidate-v1'},
                profile={'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                         'embedding_fingerprint': profile['embeddingFingerprint'],
                         'dimension': profile['dimension']},
                embed_document_chunks=lambda _body, path, **_kw: [
                    {'seq': 0, 'pos': 0, 'vector': [1.0] + [0.0] * (profile['dimension'] - 1)}])
            template = next((self.case.auto/'wiki').rglob('*.md')).read_text()
            body = template + '\nSynthetic capacity worker evidence.\n'
            (self.case.auto/'wiki'/'capacity-new-card.md').write_text(body)
            decision = self.J.select_route(str(self.case.project), action='update')
            identity = {'projectRoot': decision['projectRoot'], 'collections': decision['collections']}
            self.assertTrue(laya_incremental_docs._verified(identity,
                self.case.WIKI+'/capacity-new-card.md', body))
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            service = mock.Mock()
            original = LayaIndexRuntime.request_scan
            marks, calls, baselines = [], [], []
            def request_scan(runtime):
                side=side_db()
                marks.append(self.J.inspect_journal(self.case.journal)['newWriteSeen'])
                table=side.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='publication_revisions'").fetchone()
                baseline=(side.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()
                          if table else None)
                baselines.append(None if baseline is None else json.loads(baseline[0]))
                return original(runtime)
            with self.trace(calls), \
                    mock.patch.object(self.worker, '_route_source', return_value=roots), \
                    mock.patch.object(self.worker, '_capacity_paths',
                                      return_value=(str(self.case.catalog), str(self.case.store))), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(self.worker, '_selected_service', return_value=service), \
                    mock.patch.object(self.worker, '_higher_priority_wake', return_value=False), \
                    mock.patch.object(laya_wiki_discovery, 'observe_grants', return_value=None), \
                    mock.patch.object(laya_external_edits, 'drain', return_value={}), \
                    mock.patch.object(LayaIndexRuntime, 'request_scan', new=request_scan), \
                    mock.patch.object(self.J, 'validate_production_provider_attestation',
                                      return_value=proof['providerManifest']), \
                    mock.patch.object(self.J, '_verify_runtime_files'):
                visible = False
                # Under slow admission a wake can advance only one scan step;
                # keep a finite retry bound while still requiring publication.
                for wake in range(24):
                    result = self.worker.run(self.case.project, coordinator=controller,
                                             report={}, start_service=lambda *_a, **_kw: None)
                    catalog = result.get('catalog') or {}
                    active = catalog.get('active') or {}
                    emit('capacity-worker-wake', wake=wake + 1,
                         state=result.get('state'), reason=result.get('reason'),
                         phase=active.get('phase'), steps=active.get('steps'),
                         scanId=active.get('scan_id'), rerequested=active.get('rerequested'))
                    if result.get('state') == 'deferred' and result.get('reason') in (
                            'catalog_busy', 'deferred', 'reader_write_deadline',
                            'reader_publish_busy', 'reader_mutation_busy', 'reader_lifetime_busy'):
                        continue  # A bounded wake may defer; publication must still finish below.
                    self.assertIn(result['state'], ('indexed', 'unchanged', 'pending'), result)
                    side=side_db()
                    cursor = witness.execute('SELECT 1 FROM head h JOIN membership m '
                        'ON m.gen=h.gen AND m.scope=h.scope WHERE h.scope=? AND m.path LIKE ?',
                        (self.case.route['scopeId'], '%/capacity-new-card.md'))
                    try:
                        visible = cursor.fetchone() is not None
                    finally:
                        cursor.close()
                    side_cursor = side.execute('SELECT 1 FROM docs WHERE path=?',
                                               (self.case.WIKI+'/capacity-new-card.md',))
                    try:
                        side_visible = side_cursor.fetchone() is not None
                    finally:
                        side_cursor.close()
                    if visible and side_visible:
                        break
                self.assertTrue(visible and side_visible, result)
                tables=side.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='publication_revisions'").fetchall()
                self.assertEqual(tables,[('publication_revisions',)],
                                 'visible sidecar publication must have revision receipts')
                receipts=side.execute('SELECT value FROM publication_revisions ORDER BY revision').fetchall()
                self.assertTrue(receipts,'visible sidecar publication must record its revision')
                baseline=side.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()
                self.assertIsNotNone(baseline,'legacy sidecar must seal its preexisting rows before publication')
                baseline=json.loads(baseline[0])
                self.assertEqual(baseline['component'],'sidecar')
                self.assertEqual(baseline['kind'],'baseline')
                self.assertEqual(baseline['rows'],3)
                receipt=json.loads(receipts[-1][0])
                self.assertEqual(receipt['component'],'sidecar')
                self.assertRegex(receipt['identity'],r'^[0-9a-f]{64}$')
                meta=side.execute('SELECT * FROM meta').fetchone()
                expected_identity=hashlib.sha256(json.dumps(['sidecar',*meta],ensure_ascii=True,
                    separators=(',',':')).encode('ascii')).hexdigest()
                self.assertEqual(receipt['identity'],expected_identity)
            import laya_reader_open
            import laya_v6_reader
            with self.seed.lease.resolve_endpoint(self.case.project,action='recall',
                                                  host_root=self.case.plugin) as endpoint:
                recall_decision=self.J.select_route(str(self.case.project),action='recall')
                with laya_reader_open.inspect_warm_snapshot(endpoint,deadline=time.monotonic()+10) as observed:
                    self.assertTrue(observed.sidecar['baseline'])
                    answer=laya_v6_reader.query_v6_snapshot(observed,endpoint=endpoint,
                        prompt='capacity',decision=recall_decision,provider=None,
                        deadline=time.monotonic()+10)
                    self.assertIn(self.case.WIKI+'/capacity-new-card.md',
                                  [row['path'] for row in answer.rows])
            self.assertIn(result['state'], ('indexed', 'unchanged', 'pending'), result)
            self.assertTrue(marks and all(marks))
            self.assertTrue(baselines and all(item is not None and item['rows'] >= 3 for item in baselines),baselines)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            emit('capacity-owned-sync', result=result, managedContentOpens=len(calls))

    @contextmanager
    def _compile_warm(self):
        # The common warm fixture adds an exclusion and intentionally makes
        # the published head stale; retrieval needs an unchanged head instead.
        from laya_index_a import LayaIndexA
        from laya_index_claim_queue import ClaimQueue
        roots={self.case.WIKI:str(self.case.auto/'wiki')}
        from laya_source_catalog import LayaSourceCatalog
        with closing(LayaIndexA(self.case.db)),closing(ClaimQueue.open(self.queue)), \
                closing(LayaSourceCatalog(self.case.catalog,roots,self.case.route['scopeId'])):
            yield

    def test_compile_edit_is_durable_when_sync_owner_is_busy(self):
        import laya_host_response as response
        import laya_wiki_discovery as discovery
        import laya_external_edits
        import compile_paths as cp
        import runtime_work
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected()
            state,_grants,_lock=discovery._paths(decision)
            snapshot=discovery._raw_snapshot(decision,discovery._binding(decision)[4])
            discovery._write(state,discovery._state_doc(decision,self.case.head()[0],snapshot,{}))
            source=self.case.project/'busy-edit.md'
            source.write_text('# Synthetic edit\ncontent\n',encoding='utf8')
            payload={'cwd':str(self.case.project),'hook_event_name':'PostToolUse',
                     'tool_name':'Write','tool_input':{'file_path':str(source)},'engine':'codex'}
            with mock.patch.object(response,'__file__',str(self.case.plugin/'core/laya_host_response.py')), \
                    mock.patch.object(runtime_work,'request',return_value={'status':'queued'}) as request, \
                    mock.patch.object(laya_external_edits,'drain',return_value={}):
                with self.sync.sync_owner(self.queue) as owner:
                    self.assertIsNotNone(owner)
                    self.assertEqual(response._serve_selected_once('compile','codex',decision,payload),{})
                    observed=discovery.observe_grants(decision)
                    self.assertEqual(len(observed['grants']['sync']),1)
                    self.assertTrue(any(call.args[0]=='laya-sync' for call in request.call_args_list))
                result=self.worker._after_sync(decision,proof,observed,{'state':'unchanged'})
                self.assertEqual(result['state'],'unchanged',result)
            rows=[json.loads(line) for line in cp.under(self.case.project,cp.SOURCE_QUEUE).read_text().splitlines()]
            self.assertTrue(any(row['source']['path']=='busy-edit.md' for row in rows))

    def test_compile_edit_owner_race_after_append_queues_once(self):
        import laya_host_response as response
        import laya_wiki_discovery as discovery
        import laya_external_edits
        import wiki_compile_enqueue as enqueue
        import compile_paths as cp
        import runtime_work
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected()
            state,_grants,_lock=discovery._paths(decision)
            snapshot=discovery._raw_snapshot(decision,discovery._binding(decision)[4])
            discovery._write(state,discovery._state_doc(decision,self.case.head()[0],snapshot,{}))
            source=self.case.project/'raced-edit.md'
            source.write_text('# Synthetic edit\ncontent\n',encoding='utf8')
            payload={'cwd':str(self.case.project),'hook_event_name':'PostToolUse',
                     'tool_name':'Write','tool_input':{'file_path':str(source)},'engine':'codex'}
            path=cp.under(self.case.project,cp.SOURCE_QUEUE)
            actual=enqueue._append_jsonl;raced=[]
            with ExitStack() as rival, \
                    mock.patch.object(response,'__file__',str(self.case.plugin/'core/laya_host_response.py')), \
                    mock.patch.object(runtime_work,'request',return_value={'status':'queued'}), \
                    mock.patch.object(laya_external_edits,'drain',return_value={}):
                def append(queue,rows):
                    result=actual(queue,rows)
                    if Path(queue)==path and rows and not raced:
                        raced.append(rival.enter_context(self.sync.sync_owner(self.queue)))
                    return result
                with mock.patch.object(enqueue,'_append_jsonl',new=append):
                    self.assertEqual(response._serve_selected_once('compile','codex',decision,payload),{})
                observed=discovery.observe_grants(decision)
                self.assertEqual(len(raced),1)
                self.assertIsNone(raced[0],'competing sync must not enter between append and wake')
                rival.close()
                if observed['grants']['sync']:
                    result=self.worker._after_sync(decision,proof,observed,{'state':'unchanged'})
                    self.assertEqual(result['state'],'unchanged',result)
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(sum(row['source']['path']=='raced-edit.md' for row in rows),1)

    def test_legacy_v4_compile_edit_with_owned_enqueue_wakes(self):
        import laya_host_response as response
        import compile_paths as cp
        import runtime_work
        with self._compile_warm(),self.seed.authority():
            self.publish_v4()
            decision,_proof=self.selected()
            self.assertEqual(self.J.journal_version(decision['journalPath']),4)
            source=self.case.project/'legacy-compile-edit.md'
            source.write_text('# Synthetic legacy edit\ncontent\n',encoding='utf8')
            payload={'cwd':str(self.case.project),'hook_event_name':'PostToolUse',
                     'tool_name':'Write','tool_input':{'file_path':str(source)},'engine':'codex'}
            with mock.patch.object(response,'__file__',str(self.case.plugin/'core/laya_host_response.py')), \
                    mock.patch.object(runtime_work,'request',return_value={'status':'queued'}) as request:
                self.assertEqual(response._serve_selected_once('compile','codex',decision,payload),{})
            rows=[json.loads(line) for line in cp.under(self.case.project,cp.SOURCE_QUEUE).read_text().splitlines()]
            self.assertEqual(sum(row['source']['path']==source.name for row in rows),1)
            self.assertTrue(any(call.args[0]=='laya-compile' for call in request.call_args_list))

    def test_full_selected_compile_retrieval_rechecks_with_owned_connections(self):
        import wiki_compile_retrieval as retrieval
        import wiki_selected_laya as selected
        import laya_host_response as response
        with self._compile_warm(),self.seed.authority():
            calls=[]
            def cold(*_args):raise response._VectorUnavailable('synthetic_no_query_vector')
            with self.trace(calls),mock.patch.object(retrieval,'query_provider',return_value=cold):
                selected.wiki_matches(self.case.project,self.case.auto/'wiki','synthetic',3,200)
            self.assertGreaterEqual(len(calls),4)
            self.assertTrue(all(call['managed'] for call in calls),calls)

    def test_selected_compile_retrieval_uses_owned_runtime_and_balanced_pin(self):
        import wiki_compile_retrieval as retrieval
        import laya_host_response as response
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected();calls=[]
            with closing(sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True)) as db:
                before=db.execute('SELECT count(*) FROM pins').fetchone()[0]
            def cold(*_args):raise response._VectorUnavailable('synthetic_no_query_vector')
            with self.trace(calls),mock.patch.object(retrieval,'query_provider',return_value=cold):
                with retrieval.selected_search((decision,proof),'synthetic') as result:
                    self.assertEqual(len(result),4)
            self.assertGreaterEqual(len(calls),4)
            self.assertTrue(all(call['managed'] for call in calls),calls)
            with closing(sqlite3.connect(self.case.db.as_uri()+'?mode=ro',uri=True)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM pins').fetchone()[0],before)

    def test_selected_compile_retrieval_refuses_busy_sync_owner_before_db_open(self):
        import wiki_compile_retrieval as retrieval
        from resource_deferred import ResourceDeferred
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected();calls=[]
            import laya_host_response as response
            def cold(*_args):raise response._VectorUnavailable('synthetic_no_query_vector')
            with self.sync.sync_owner(self.queue) as owner,self.trace(calls), \
                    mock.patch.object(retrieval,'query_provider',return_value=cold):
                self.assertIsNotNone(owner)
                with self.assertRaises(ResourceDeferred):
                    with retrieval.selected_search((decision,proof),'synthetic'):pass
            self.assertEqual(calls,[])

    def test_selected_compile_retrieval_close_failure_keeps_owner_until_drain(self):
        import wiki_compile_retrieval as retrieval
        import laya_host_response as response
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected();state={'armed':False,'blocked':True,'failed':0}
            native=self.W._OwnedSQLite._native_close_locked
            def cold(*_args):raise response._VectorUnavailable('synthetic_no_query_vector')
            def close(db):
                ref=getattr(db,'_window_owner',None);owner=ref() if ref is not None else None
                if (state['armed'] and state['blocked'] and type(owner) is self.W._OwnedConnection
                        and owner.component.role=='base'):
                    state['failed']+=1
                    raise sqlite3.OperationalError('injected_compile_close_failure')
                return native(db)
            try:
                with mock.patch.object(retrieval,'query_provider',return_value=cold), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected_compile_close_failure'):
                        with retrieval.selected_search((decision,proof),'synthetic'):
                            state['armed']=True
                    self.assertGreater(state['failed'],0)
                    self.assertGreater(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNone(successor)
                    state['blocked']=False
                    self.W.drain_pending(deadline=time.monotonic()+3)
                    self.assertEqual(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
            finally:
                state['blocked']=False
                self.W.drain_pending(deadline=time.monotonic()+3)

    def test_actual_discovery_pass_uses_owned_base_connections(self):
        import laya_external_edits
        import laya_wiki_discovery as discovery
        import laya_wiki_backfill as backfill
        import wiki_selected_laya as selected
        with self.fixture.F.warm(),self.seed.authority():
            decision,proof=self.selected()
            state,_grants,_lock=discovery._paths(decision)
            raw=discovery._binding(decision)[4]
            snapshot=discovery._raw_snapshot(decision,raw)
            discovery._write(state,discovery._state_doc(decision,self.case.head()[0],snapshot,{}))
            observed={'grants':{'sync':[],'session_start':[]},
                      'provisional':{'sync':[],'session_start':[]},'engines':{}}
            calls=[]
            def nested_compile_proof(*_args):
                bound=selected.selected(self.case.project)
                selected.recheck(bound)
                return None
            with self.trace(calls),mock.patch.object(laya_external_edits,'drain',return_value={}), \
                    mock.patch.object(backfill,'discover',side_effect=nested_compile_proof):
                result=self.worker._after_sync(decision,proof,observed,{'state':'unchanged'})
            base=[call for call in calls if call['path']==str(self.case.db)]
            self.assertGreaterEqual(len(base),2)
            self.assertTrue(all(call['managed'] for call in base),base)
            self.assertEqual(result['state'],'unchanged',result)

    def test_discovery_real_enqueue_reuses_owned_compile_proof(self):
        import laya_external_edits
        import laya_wiki_discovery as discovery
        import runtime_work
        import compile_paths as cp
        with self.fixture.F.warm(),self.seed.authority():
            decision,proof=self.selected()
            state,_grants,_lock=discovery._paths(decision)
            snapshot=discovery._raw_snapshot(decision,discovery._binding(decision)[4])
            discovery._write(state,discovery._state_doc(decision,self.case.head()[0],snapshot,{}))
            (self.case.project/'nested-change.md').write_text('# Synthetic\nChanged source.\n',encoding='utf8')
            grant=discovery.record_grant(decision,'sync',engine='codex')
            self.assertTrue(discovery.activate_grant(decision,'sync',grant))
            observed=discovery.observe_grants(decision);calls=[]
            with self.trace(calls),mock.patch.object(laya_external_edits,'drain',return_value={}), \
                    mock.patch.object(runtime_work,'request',return_value={'status':'queued'}) as request:
                result=self.worker._after_sync(decision,proof,observed,{'state':'unchanged'})
            self.assertEqual(result['state'],'unchanged',result)
            self.assertTrue(all(call['managed'] for call in calls),calls)
            queue=cp.under(self.case.project,cp.SOURCE_QUEUE)
            rows=[json.loads(line) for line in queue.read_text().splitlines()]
            self.assertTrue(any(row['source']['path']=='nested-change.md' for row in rows),rows)
            self.assertTrue(any(call.args[0]=='laya-compile' for call in request.call_args_list))

    def test_discovery_close_failure_retains_owner_until_creator_drains(self):
        import laya_external_edits
        import laya_wiki_discovery as discovery
        import laya_wiki_backfill as backfill
        with self.fixture.F.warm(),self.seed.authority():
            decision,proof=self.selected()
            state_path,_grants,_lock=discovery._paths(decision)
            snapshot=discovery._raw_snapshot(decision,discovery._binding(decision)[4])
            discovery._write(state_path,discovery._state_doc(decision,self.case.head()[0],snapshot,{}))
            observed={'grants':{'sync':[],'session_start':[]},
                      'provisional':{'sync':[],'session_start':[]},'engines':{}}
            state={'armed':False,'blocked':True,'failures':0}
            native=self.W._OwnedSQLite._native_close_locked
            original=discovery._read_view
            def read_view(*args,**kwargs):
                state['armed']=True
                return original(*args,**kwargs)
            def close(db):
                ref=getattr(db,'_window_owner',None);owner=ref() if ref is not None else None
                if (state['armed'] and state['blocked'] and type(owner) is self.W._OwnedConnection
                        and owner.component.role=='base'):
                    state['failures']+=1
                    raise sqlite3.OperationalError('injected_discovery_close_failure')
                return native(db)
            try:
                with mock.patch.object(laya_external_edits,'drain',return_value={}), \
                        mock.patch.object(backfill,'discover',return_value=None), \
                        mock.patch.object(discovery,'_read_view',side_effect=read_view), \
                        mock.patch.object(self.W._OwnedSQLite,'_native_close_locked',new=close):
                    with self.assertRaisesRegex(sqlite3.OperationalError,'injected_discovery_close_failure'):
                        self.worker._after_sync(decision,proof,observed,{'state':'unchanged'})
                    self.assertGreater(state['failures'],0)
                    self.assertGreater(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNone(successor)
                    state['blocked']=False
                    self.W.drain_pending(deadline=time.monotonic()+3)
                    self.assertEqual(self.W.local_pending_count(),0)
                    with self.sync.sync_owner(self.queue) as successor:self.assertIsNotNone(successor)
            finally:
                state['blocked']=False
                self.W.drain_pending(deadline=time.monotonic()+3)

    def test_discovery_external_inbox_full_proofs_use_owned_readers(self):
        import laya_external_edits
        import laya_wiki_discovery
        import runtime_work
        import wiki_selected_laya
        with self.fixture.F.warm(), self.seed.authority():
            decision, proof = self.selected()
            inbox = laya_external_edits._inbox_path(decision)
            inbox.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            laya_wiki_discovery._write(inbox, {**laya_external_edits._binding(decision),
                                              'edits': [], 'seen': [], 'tokens': {}})
            calls = []
            with self.trace(calls):
                result = self.worker._after_sync(decision, proof, None, {'state': 'unchanged'})
            self.assertEqual(result, {'state': 'unchanged'})
            self.assertGreaterEqual(len(calls), 4)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            calls = []
            with self.sync.sync_owner(self.queue) as owner, self.trace(calls), \
                    mock.patch.object(runtime_work, 'request', return_value={'status': 'queued'}) as request:
                wiki_selected_laya.request_compile(self.case.project, (decision, proof), owner=owner)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call['managed'] for call in calls), calls)
            request.assert_called_once()

    def test_external_edit_inbox_enqueues_with_existing_sync_owner(self):
        import laya_external_edits as external
        import laya_wiki_discovery as discovery
        import compile_paths as cp
        import runtime_work
        with self._compile_warm(),self.seed.authority():
            decision,proof=self.selected()
            source=self.case.project/'external-owned-edit.md'
            source.write_text('# External edit\nSynthetic content.\n',encoding='utf8')
            blob,_revision=external._revision(source)
            row={'path':source.name,'blob':blob,'host':'codex'}
            inbox=external._inbox_path(decision)
            inbox.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            discovery._write(inbox,{**external._binding(decision),'edits':[row],
                                    'seen':[],'tokens':{source.name:'a'*32}})
            with mock.patch.object(runtime_work,'request',return_value={'status':'queued'}) as request:
                result=self.worker._after_sync(decision,proof,None,{'state':'unchanged'})
            self.assertEqual(result,{'state':'unchanged'})
            remaining=external._read_box(inbox,decision)
            self.assertEqual(remaining['edits'],[])
            self.assertEqual(remaining['seen'],[row])
            queue=cp.under(self.case.project,cp.SOURCE_QUEUE)
            records=[json.loads(line) for line in queue.read_text().splitlines()]
            self.assertEqual(sum(record['source']['path']==source.name for record in records),1)
            self.assertTrue(any(call.args[0]=='laya-compile' for call in request.call_args_list))

    def _capacity_sidecar_fixture(self, *, recovery):
        import laya_incremental_docs
        import laya_wiki_discovery
        from laya_source_catalog import LayaSourceCatalog
        class ReachedCapacity(Exception):
            pass
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        sidecar = self.seed.sidecar
        with self.fixture.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']):
            if recovery == 'lost':
                with sqlite3.connect(self.case.db) as base:
                    base.execute("INSERT INTO schema_meta(key,value) VALUES('reader-sidecar-required:v1','1')")
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(sidecar)+suffix).unlink(missing_ok=True)
            if recovery not in ('create', 'interrupt', 'closefail', 'busy', 'busy_pending',
                                'physical_busy', 'physical_busy_pending', 'lost'):
                import laya_initial_build
                identity = laya_initial_build.read(self.case.state)['identity']
                with closing(sqlite3.connect(sidecar, isolation_level=None)) as db:
                    self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone(), ('delete',))
                    laya_incremental_docs._setup(db, identity)
                    if recovery == 'empty':
                        # A crash can leave material from the previous bundle.
                        db.execute('DROP TRIGGER publication_revision_insert_seal')
                        db.execute('DROP TRIGGER publication_revision_update_seal')
                        db.execute('DROP TRIGGER publication_revision_delete_seal')
                        db.execute('DROP TABLE publication_revisions')
                    if recovery == 'nonempty':
                        db.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                   ('old.md', 'a'*64, 'old', 'body', identity['vectorProfileId'], 1, 'scan'))
                sidecar.chmod(0o600)
            before = sidecar.read_bytes() if recovery == 'nonempty' else None
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'],
                'dimension': profile['dimension']})
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            observed = []
            def capacity(*_args, **_kwargs):
                if recovery in ('busy', 'busy_pending', 'physical_busy', 'physical_busy_pending'):
                    reason = 'reader_physical_busy' if recovery.startswith('physical_') else 'reader_write_deadline'
                    error = self.W.ReaderWindowBusy(reason)
                    if recovery.endswith('_pending'):
                        error.rollback_pending = True
                    raise error
                observed.append((sidecar.exists(), self.J.inspect_journal(self.case.journal)['newWriteSeen']))
                raise ReachedCapacity()
            with mock.patch.object(self.worker, '_route_source', return_value=roots), \
                    mock.patch.object(self.worker, '_capacity_paths',
                                      return_value=(str(self.case.catalog), str(self.case.store))), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(laya_wiki_discovery, 'observe_grants', return_value=None), \
                    mock.patch.object(self.worker, '_member_diagnosis', return_value=None), \
                    mock.patch.object(self.worker, '_sync_capacity', side_effect=capacity):
                if recovery in ('busy', 'physical_busy'):
                    result = self.worker.run(self.case.project, coordinator=controller,
                                             report={}, start_service=lambda *_a, **_kw: None)
                    self.assertEqual((result['state'], result['reason']),
                                     ('deferred', 'reader_physical_busy' if recovery == 'physical_busy'
                                      else 'reader_write_deadline'))
                    return
                elif recovery in ('busy_pending', 'physical_busy_pending'):
                    reason = 'reader_physical_busy' if recovery == 'physical_busy_pending' else 'reader_write_deadline'
                    with self.assertRaisesRegex(self.W.ReaderWindowBusy, reason) as caught:
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                    self.assertTrue(caught.exception.rollback_pending)
                    return
                elif recovery == 'closefail':
                    native = self.W._OwnedSQLite._native_close_locked
                    failed = []
                    def close(db):
                        owner_ref = getattr(db, '_window_owner', None)
                        owned = owner_ref() if owner_ref is not None else None
                        if owned is not None and hasattr(owned, 'private_material') and not failed:
                            failed.append(True)
                            raise sqlite3.OperationalError('injected_sidecar_material_close_failure')
                        return native(db)
                    with mock.patch.object(self.W._OwnedSQLite, '_native_close_locked', new=close):
                        with self.assertRaisesRegex(sqlite3.OperationalError,
                                                    'injected_sidecar_material_close_failure'):
                            self.worker.run(self.case.project, coordinator=controller,
                                            report={}, start_service=lambda *_a, **_kw: None)
                    self.assertEqual(failed, [True])
                    self.assertFalse(sidecar.exists())
                    self.assertEqual(self.W.local_pending_count(), 0)
                    with self.sync.sync_owner(self.queue) as successor:
                        self.assertIsNotNone(successor)
                    with self.assertRaises(ReachedCapacity):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                elif recovery == 'interrupt':
                    import laya_writer_scope
                    with mock.patch.object(laya_writer_scope, 'adopt_created_sidecar',
                                           side_effect=RuntimeError('injected_after_rename')):
                        with self.assertRaisesRegex(RuntimeError, 'injected_after_rename'):
                            self.worker.run(self.case.project, coordinator=controller,
                                            report={}, start_service=lambda *_a, **_kw: None)
                    self.assertTrue(sidecar.is_file())
                    self.assertFalse(any(Path(str(sidecar)+suffix).exists()
                                         for suffix in ('-wal','-shm','-journal')))
                    self.assertTrue(self.J.inspect_journal(self.case.journal)['newWriteSeen'])
                    with self.assertRaises(ReachedCapacity):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                elif recovery == 'lost':
                    with self.assertRaisesRegex(ValueError, 'reader_sidecar_revision_unproved'):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                    self.assertFalse(sidecar.exists())
                    self.assertEqual(observed, [])
                    return
                elif recovery == 'nonempty':
                    with self.assertRaisesRegex(ValueError, 'normal_writer_sidecar_creation_changed'):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
                else:
                    with self.assertRaises(ReachedCapacity):
                        self.worker.run(self.case.project, coordinator=controller,
                                        report={}, start_service=lambda *_a, **_kw: None)
            if recovery == 'nonempty':
                self.assertEqual(observed, [])
                self.assertEqual(sidecar.read_bytes(), before)
                self.assertFalse(any(Path(str(sidecar)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
                return
            self.assertEqual(observed, [(True, True)])
            with closing(sqlite3.connect(sidecar.as_uri()+'?mode=ro', uri=True)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone(), (1,))
                if recovery == 'empty':
                    baseline=db.execute('SELECT value FROM publication_revisions WHERE revision=0').fetchone()
                    self.assertIsNotNone(baseline)
                    self.assertEqual(json.loads(baseline[0])['rows'],0)

    def test_unmarked_writer_cannot_create_sidecar(self):
        import laya_writer_scope as normal
        with self.fixture.F.warm(), self.seed.authority():
            for suffix in ('','-wal','-shm','-journal'):
                Path(str(self.seed.sidecar)+suffix).unlink(missing_ok=True)
            plan=self.P.capture_writer_plan(self.case.project,executing_root=self.case.plugin)
            before=set(self.case.state.iterdir())
            with self.sync.sync_owner(self.queue) as owner:
                with normal.worker_writer_scope(plan,queue_owner=owner,operation_timeout=5) as writer:
                    component=self.W.component_for(writer,'sidecar')
                    with self.assertRaisesRegex(ValueError,'normal_writer_intent_required'):
                        self.W.create_sidecar_owned(component,deadline=time.monotonic()+3)
            self.assertEqual(set(self.case.state.iterdir()),before)
            self.assertFalse(self.seed.sidecar.exists())

    def test_capacity_worker_creates_absent_sidecar_after_fence(self):
        self._capacity_sidecar_fixture(recovery='create')

    def test_capacity_worker_refuses_to_recreate_lost_required_sidecar(self):
        self._capacity_sidecar_fixture(recovery='lost')

    def test_capacity_worker_defers_writer_deadline(self):
        self._capacity_sidecar_fixture(recovery='busy')

    def test_capacity_worker_propagates_pending_rollback(self):
        self._capacity_sidecar_fixture(recovery='busy_pending')

    def test_capacity_worker_defers_physical_window_busy(self):
        self._capacity_sidecar_fixture(recovery='physical_busy')

    def test_capacity_worker_propagates_physical_busy_pending_rollback(self):
        self._capacity_sidecar_fixture(recovery='physical_busy_pending')

    def test_capacity_worker_resumes_empty_cold_sidecar_after_fence(self):
        self._capacity_sidecar_fixture(recovery='empty')

    def test_capacity_worker_rejects_nonempty_cold_delete_sidecar(self):
        self._capacity_sidecar_fixture(recovery='nonempty')

    def test_capacity_worker_resumes_after_interrupted_sidecar_rename(self):
        self._capacity_sidecar_fixture(recovery='interrupt')

    def test_capacity_worker_drains_failed_sidecar_material_close(self):
        self._capacity_sidecar_fixture(recovery='closefail')

    def _capacity_worker_sigkill_at_sidecar_rename(self, mode):
        import laya_wiki_discovery
        from laya_source_catalog import LayaSourceCatalog
        class ReachedCapacity(Exception):
            pass
        roots = {self.case.WIKI: str(self.case.auto/'wiki')}
        sidecar = self.seed.sidecar
        with self.fixture.F.warm(), self.seed.authority(), \
                LayaSourceCatalog(self.case.catalog, roots, self.case.route['scopeId']):
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(sidecar)+suffix).unlink(missing_ok=True)
            payload = self.case.home/'normal-sidecar-crash.json'
            payload.write_text(json.dumps({'home': os.environ['HOME'],
                'runtime': os.environ['MEMVANE_TEST_RUNTIME_ROOT'], 'plugin': str(self.case.plugin),
                'project': str(self.case.project), 'proof': self.case.proof, 'mode': mode}))
            child = subprocess.run([sys.executable, '-B', '-I', '-c', NORMAL_CREATE_CRASH,
                                    str(ROOT), str(payload)], capture_output=True,
                                   encoding='utf8', timeout=40)
            self.assertEqual(child.returncode, -signal.SIGKILL, child.stdout + child.stderr)
            event = json.loads(child.stdout.strip())
            self.assertEqual((event['event'], event['mode']), ('rename-checkpoint', mode))
            orphan = self.case.state/event['source']
            if mode == 'before':
                self.assertFalse(sidecar.exists())
                self.assertTrue(orphan.is_file())
                stat = orphan.stat()
                witness = (orphan.read_bytes(), stat.st_dev, stat.st_ino)
                self.assertEqual([stat.st_dev, stat.st_ino], event['node'])
            else:
                self.assertTrue(sidecar.is_file())
                self.assertFalse(orphan.exists())
                self.assertEqual([sidecar.stat().st_dev, sidecar.stat().st_ino], event['node'])
                self.assertFalse(any(Path(str(sidecar)+suffix).exists()
                                     for suffix in ('-wal','-shm','-journal')))
            _, proof = self.selected()
            profile = proof['vectorProfile']
            provider = SimpleNamespace(manifest={}, profile={
                'profile_id': profile['profileId'], 'model_id': profile['modelId'],
                'embedding_fingerprint': profile['embeddingFingerprint'], 'dimension': profile['dimension']})
            controller = SimpleNamespace(activity=lambda *_args: nullcontext())
            def reached(*_args, **_kwargs):
                self.assertTrue(sidecar.exists())
                raise ReachedCapacity()
            with mock.patch.object(self.worker, '_route_source', return_value=roots), \
                    mock.patch.object(self.worker, '_capacity_paths',
                                      return_value=(str(self.case.catalog), str(self.case.store))), \
                    mock.patch.object(self.worker, 'qa_sync_barrier', return_value=None), \
                    mock.patch.object(self.worker, 'ReviewedLocalQmdProvider', return_value=provider), \
                    mock.patch.object(laya_wiki_discovery, 'observe_grants', return_value=None), \
                    mock.patch.object(self.worker, '_member_diagnosis', return_value=None), \
                    mock.patch.object(self.worker, '_sync_capacity', side_effect=reached):
                with self.assertRaises(ReachedCapacity):
                    self.worker.run(self.case.project, coordinator=controller,
                                    report={}, start_service=lambda *_a, **_kw: None)
            if mode == 'before':
                stat = orphan.stat()
                self.assertEqual((orphan.read_bytes(), stat.st_dev, stat.st_ino), witness)
            emit('normal-sidecar-sigkill', phase=mode, actualKill=child.returncode,
                 targetPresent=sidecar.is_file(), orphanPreserved=(mode == 'before'))

    def test_capacity_worker_recovers_fresh_sigkill_before_sidecar_rename(self):
        self._capacity_worker_sigkill_at_sidecar_rename('before')

    def test_capacity_worker_recovers_fresh_sigkill_after_sidecar_rename(self):
        self._capacity_worker_sigkill_at_sidecar_rename('after')

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

    def test_true_no_aux_first_open_uses_read_only_owned_proof(self):
        with self.seed.authority():
            from laya_index_a import LayaIndexA
            from laya_index_claim_queue import ClaimQueue
            # The seed's earlier RO proof can leave warm auxiliaries. Only
            # actual last-writer close establishes this cold baseline.
            with closing(LayaIndexA(self.case.db)),closing(ClaimQueue.open(self.queue)):pass
            self.assertFalse(Path(str(self.case.db)+'-wal').exists())
            files=existing.existing.existing.existing.files
            before={str(p):files(p) for p in (self.case.db,self.queue)};calls=[]
            with self.trace(calls):
                _decision,proof=self.selected()
            emit('worker-first-cold-proof',contentConnects=calls)
            self.assertTrue(proof['productRouteAllowed'])
            self.assertEqual(len(calls),2);self.assertTrue(all(c['managed'] for c in calls))
            for path in (self.case.db,self.queue):
                after=files(path)
                self.assertEqual(after[''],before[str(path)][''])
                self.assertEqual(after['-journal'],before[str(path)]['-journal'])

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

    def test_markerless_hot_companion_copy_uses_existing_executing_proof_contract(self):
        with self.fixture.F.warm(),self.seed.authority(),self.markerless():
            self.publish_v4()
            companion=self.case.home/'synthetic-approved-companion'
            # Keep the existing fixture's explicit executing-copy authority
            # boundary; the real J proof must retain and approve its result.
            with mock.patch.object(self.J,'_verified_executing_root',return_value=str(companion)):
                decision=self.worker.select_route(str(self.case.project),action='update')
                baseline=self.worker.verify_selected_route(decision)
                self.assertTrue(baseline['productRouteAllowed'])
                self.assertEqual(baseline['executingPluginRoot'],str(companion))
                self.crash_hot();error=None;proof=None
                try:_decision,proof=self.selected()
                except Exception as exc:error=str(exc)
                emit('worker-markerless-hot-companion',baselineApproved=True,error=error,selected=proof is not None)
                self.assertIsNone(error)
                self.assertEqual(proof,baseline)

    def test_commit_unlink_during_source_guard_retries_once_and_churn_is_typed(self):
        with self.seed.authority():
            for moment,churn in ((m,c) for m in ('open','fstat') for c in (False,True)):
                with self.subTest(moment=moment,churn=churn):
                    self.publish_v4();side=Path(str(self.case.journal)+'-journal')
                    with closing(sqlite3.connect(self.case.journal,isolation_level=None)) as writer:
                        def begin():
                            writer.execute('BEGIN IMMEDIATE')
                            writer.execute('UPDATE selection SET epoch=epoch+1,new_write_seen=1 WHERE id=1')
                        begin();self.assertTrue(side.exists())
                        open_file=os.open;fstat=os.fstat;opens=[];commits=[];inode=[None]
                        def commit():
                            writer.commit();commits.append(True)
                            self.assertFalse(side.exists())
                        def opening(path,*args,**kwargs):
                            if Path(path)==side:
                                if opens and churn:begin()
                                fd=open_file(path,*args,**kwargs)
                                opens.append(True);inode[0]=fstat(fd).st_ino
                                if moment=='open':commit()
                                return fd
                            return open_file(path,*args,**kwargs)
                        def inspect(fd):
                            result=fstat(fd)
                            if moment=='fstat' and result.st_ino==inode[0] and len(commits)<len(opens):commit()
                            return result
                        result=None;error=None
                        with self.J.read_only_journals(),mock.patch.object(os,'open',side_effect=opening), \
                                mock.patch.object(os,'fstat',side_effect=inspect):
                            try:result=self.J.journal_version(self.case.journal)
                            except Exception as exc:error=exc
                        emit('worker-journal-commit-unlink',moment=moment,churn=churn,actualCommits=len(commits),
                             guardOpens=len(opens),result=result,errorType=type(error).__name__,error=str(error))
                        if churn:
                            self.assertIsInstance(error,self.J.JournalEvidenceError)
                            self.assertEqual(str(error),'journal_changed')
                            self.assertEqual(len(commits),2)
                        else:
                            self.assertIsNone(error);self.assertEqual(result,4)
                            self.assertEqual(len(commits),1)

    def test_source_guard_keeps_hardlinked_rollback_journal_rejected(self):
        with self.seed.authority():
            for moment in ('initial','named'):
                with self.subTest(moment=moment):
                    self.publish_v4();side=Path(str(self.case.journal)+'-journal')
                    alias=side.with_name('test-hardlink-journal-alias')
                    with closing(sqlite3.connect(self.case.journal,isolation_level=None)) as writer:
                        writer.execute('BEGIN IMMEDIATE')
                        writer.execute('UPDATE selection SET epoch=epoch+1 WHERE id=1')
                        stat=Path.lstat;linked=[]
                        def named(path,*args,**kwargs):
                            if path==side and moment=='named' and not linked:
                                os.link(side,alias);linked.append(True)
                            return stat(path,*args,**kwargs)
                        if moment=='initial':os.link(side,alias)
                        try:
                            with self.J.read_only_journals(),mock.patch.object(Path,'lstat',new=named), \
                                    self.assertRaisesRegex(self.J.JournalEvidenceError,'unsafe_journal_file'):
                                self.J.journal_version(self.case.journal)
                        finally:
                            if os.path.lexists(alias):alias.unlink()
                            writer.rollback()

if __name__=='__main__':unittest.main()
