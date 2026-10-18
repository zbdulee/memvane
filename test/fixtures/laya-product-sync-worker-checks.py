#!/usr/bin/env python3
"""Selected worker wiring only; no model, QMD, socket or host is started."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_product_sync_worker as worker
import runtime_work
from context_learning.local_cycle import ResourceDeferred
from laya_index_a import IndexContractError


class Coordinator:
    def __init__(self):
        self.lanes=[];self.checks=[]

    @contextmanager
    def activity(self,lane,policy,report):
        self.lanes.append((lane,policy,report))
        yield 1

    def check(self,policy,report):
        self.checks.append((policy,report))
        return {'ready':True}


class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name).resolve();self.project.chmod(0o700)
        self.decision={'sourceRoot':str(self.project),'scopeId':'a'*32,
            'dbPath':str(self.project/'index.sqlite'),'queuePath':str(self.project/'queue.sqlite'),
            'dbIncarnation':'b'*32,'journalPath':str(self.project/'install-journal.sqlite')}
        self.profile={'profileId':'c'*64,'modelId':'synthetic','embeddingFingerprint':'d'*64,
                      'dimension':2}
        self.proof={'productRouteAllowed':True,'selectionEpoch':7,
                    'providerManifest':{'synthetic':True},'vectorProfile':self.profile}
        self.refreshed={**self.proof,'selectionEpoch':8}

    def wiring(self,*,verify=None,sync=None,activity=None,batch=None):
        batch=batch or {'synthetic':'complete'}
        controller=activity or Coordinator()
        controller.root=self.project
        provider=Mock(profile={'profile_id':self.profile['profileId'],
            'model_id':self.profile['modelId'],
            'embedding_fingerprint':self.profile['embeddingFingerprint'],
            'dimension':self.profile['dimension']},
            manifest={'recipeId':'qmd-regex-token-chunks-candidate-v1'})
        service=Mock()
        context=MagicMock();context.__enter__.return_value=context
        calls=[]
        def start(manifest,*,admit):
            calls.append(('start',manifest))
            self.assertTrue(admit())
            return service
        def complete(_runtime,*,before_publish,vector_batch,require_vector):
            self.assertTrue(require_vector)
            self.assertEqual(vector_batch,batch)
            before_publish()
            return {'state':'indexed'}
        patches=[patch.object(worker,'_selected',return_value=(self.decision,self.proof)),
            patch.object(worker,'ReviewedLocalQmdProvider',return_value=provider),
            patch.object(worker,'resource_report',return_value={'ready':True}),
            patch.object(worker,'verify_worker_attestation',return_value=self.proof['providerManifest']),
            patch.object(worker,'verify_selected_route',side_effect=verify or
                         [self.proof,self.proof,self.proof,self.proof,self.refreshed,self.refreshed]),
            patch.object(worker,'LayaIndexRuntime',return_value=context),
            patch.object(worker,'prepare_candidate_batch_off_hook',return_value=batch),
            patch.object(worker,'cas_mark_new_write',return_value={'epoch':8,'newWriteSeen':True}),
            patch.object(worker,'sync_full',side_effect=sync or complete)]
        return controller,service,calls,start,patches

    def test_installer_proof_admission_batch_and_cas_precede_ack(self):
        controller,service,calls,start,patches=self.wiring()
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,{'state':'indexed'})
        self.assertEqual(controller.lanes[0][0],'provider')
        self.assertEqual(len(controller.checks),1)
        self.assertEqual(calls,[('start',self.proof['providerManifest'])])
        service.stop.assert_called_once_with()

    def test_stale_selected_proof_stops_owned_service_before_sync(self):
        def forbidden(*_args,**_kwargs):self.fail('stale route reached sync')
        controller,service,_calls,start,patches=self.wiring(
            verify=[self.proof,{**self.proof,'selectionEpoch':9}],sync=forbidden)
        with self._patches(patches):
            with self.assertRaisesRegex(IndexContractError,'selected_proof_changed'):
                worker.run(self.project,coordinator=controller,start_service=start)
        service.stop.assert_called_once_with()

    def test_resource_refusal_returns_deferred_without_model_start(self):
        class Refusing(Coordinator):
            @contextmanager
            def activity(self,*_args):
                raise ResourceDeferred('learning_deferred_resource')
                yield
        controller,service,calls,start,patches=self.wiring(activity=Refusing())
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result['state'],'deferred')
        self.assertEqual(calls,[])
        service.stop.assert_not_called()

    def test_new_index_wake_yields_before_sync_and_stops_service(self):
        runtime_work.WakeQueue(self.project).put('index','',now=0)
        def forbidden(*_args,**_kwargs):self.fail('priority work reached sync')
        controller,service,_calls,start,patches=self.wiring(sync=forbidden)
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,{'state':'deferred','reason':'laya_sync_yield_priority'})
        service.stop.assert_called_once_with()

    def test_runtime_wake_retains_deferred_selected_sync(self):
        with patch('laya_product_sync_worker.run',return_value={'state':'deferred'}):
            self.assertFalse(runtime_work.dispatch({'kind':'laya-sync','target':str(self.project)}))
        with patch('laya_product_sync_worker.run',return_value={'state':'unchanged'}):
            self.assertTrue(runtime_work.dispatch({'kind':'laya-sync','target':str(self.project)}))

    # RQA-RACE-01 Part B QA-only barrier. A synthetic fixture marker stands in
    # for test/fixtures/release-qa-fixture.py; no host or model is started.
    NONCE='e'*32
    BATCH={'source_manifest':'1'*64}

    def arm(self,**override):
        root=self.project/'fixture';root.mkdir(mode=0o700)
        project=root/'project';project.mkdir()
        for name,value in (('.qa-fixture-owner',{'fixtureId':'f1','uid':os.getuid()}),
                           ('MANIFEST.json',{'schema':'qmd-release-qa-fixture-v1','fixtureId':'f1',
                                             'root':str(root),'paths':{'project':str(project)}})):
            (root/name).write_text(json.dumps(value));(root/name).chmod(0o644)
        armed={'schema':'laya-qa-sync-barrier-v1','fixtureId':'f1','projectRoot':str(project),
               'scopeId':self.decision['scopeId'],'dbIncarnation':self.decision['dbIncarnation'],
               'nonce':self.NONCE,'timeoutSeconds':30,**override}
        arm=root/worker.QA_BARRIER_ARM
        with os.fdopen(os.open(arm,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as out:
            json.dump(armed,out)
        self.decision['projectRoot']=str(project)
        return root,arm

    def record(self,root,suffix):
        path=root/('.qa-laya-sync-barrier-%s.%s'%(self.NONCE,suffix))
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        return json.loads(path.read_text())

    def test_qa_barrier_absent_by_default_reads_no_file(self):
        controller,_service,_calls,start,patches=self.wiring()
        environ={k:v for k,v in os.environ.items() if k!=worker.QA_BARRIER_ENV}
        with self._patches(patches),patch.dict(os.environ,environ,clear=True),\
                patch.object(worker,'_qa_json',side_effect=AssertionError('read')):
            self.assertEqual(worker.run(self.project,coordinator=controller,start_service=start),
                             {'state':'indexed'})

    def test_qa_barrier_holds_exact_scope_once_and_records_completion_order(self):
        root,arm=self.arm()
        order=[]
        def complete(_runtime,*,before_publish,vector_batch,require_vector):
            order.append(vector_batch['source_manifest'])
            return {'state':'unchanged','generation':'g-main','claim':None}
        controller,_service,_calls,start,patches=self.wiring(sync=complete,batch=self.BATCH)
        results=[]
        with self._patches(patches),patch.dict(os.environ,{worker.QA_BARRIER_ENV:str(arm)}):
            thread=threading.Thread(target=lambda:results.append(
                worker.run(self.project,coordinator=controller,start_service=start)))
            thread.start()
            held=root/('.qa-laya-sync-barrier-%s.held'%self.NONCE)
            deadline=time.monotonic()+10
            while not held.exists() and time.monotonic()<deadline:time.sleep(.01)
            time.sleep(.3)
            self.assertTrue(thread.is_alive())
            self.assertEqual(order,[])
            self.assertEqual(self.record(root,'held')['sourceManifest'],'1'*64)
            released=time.time()
            (root/('.qa-laya-sync-barrier-%s.release'%self.NONCE)).write_text('')
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results,[{'state':'unchanged','generation':'g-main','claim':None}])
        done=self.record(root,'done')
        self.assertEqual((done['state'],done['generation'],done['error']),('unchanged','g-main',None))
        self.assertGreaterEqual(done['finishedAt'],done['releasedAt'])
        self.assertGreaterEqual(done['releasedAt'],released-1)
        # The nonce is consumed: a later attempt never waits or rewrites evidence.
        controller,_service,_calls,start,patches=self.wiring(sync=complete,batch=self.BATCH)
        with self._patches(patches),patch.dict(os.environ,{worker.QA_BARRIER_ENV:str(arm)}):
            worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(order,['1'*64,'1'*64])
        self.assertEqual(self.record(root,'done'),done)

    def test_qa_barrier_records_stale_refusal_of_released_old_attempt(self):
        root,arm=self.arm()
        (root/('.qa-laya-sync-barrier-%s.release'%self.NONCE)).write_text('')
        def stale(*_args,**_kwargs):raise IndexContractError('vector_batch_stale')
        controller,service,_calls,start,patches=self.wiring(sync=stale,batch=self.BATCH)
        with self._patches(patches),patch.dict(os.environ,{worker.QA_BARRIER_ENV:str(arm)}):
            with self.assertRaisesRegex(IndexContractError,'vector_batch_stale'):
                worker.run(self.project,coordinator=controller,start_service=start)
        done=self.record(root,'done')
        self.assertEqual((done['state'],done['reason'],done['error']),
                         (None,'vector_batch_stale','IndexContractError'))
        service.stop.assert_called_once_with()

    def test_qa_barrier_timeout_defers_without_writes(self):
        root,arm=self.arm(timeoutSeconds=1)
        def forbidden(*_args,**_kwargs):self.fail('timed-out barrier reached sync')
        controller,_service,_calls,start,patches=self.wiring(sync=forbidden,batch=self.BATCH)
        with self._patches(patches),patch.dict(os.environ,{worker.QA_BARRIER_ENV:str(arm)}):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,{'state':'deferred','reason':'laya_qa_barrier_timeout'})
        self.assertEqual(self.record(root,'done')['error'],'ResourceDeferred')

    def test_qa_barrier_other_project_is_untouched(self):
        _root,arm=self.arm()
        self.decision['projectRoot']=str(self.project)
        self.assertIsNone(worker.qa_sync_barrier(self.decision,environ={worker.QA_BARRIER_ENV:str(arm)}))

    def test_qa_barrier_unsafe_or_mismatched_arm_fails_before_model_start(self):
        for case in ('mode','scope','fixture','name'):
            with self.subTest(case=case):
                self.tearDown_fixture()
                root,arm=self.arm(**({'scopeId':'0'*32} if case=='scope' else {}))
                if case=='mode':arm.chmod(0o644)
                if case=='fixture':(root/'.qa-fixture-owner').write_text(json.dumps({'fixtureId':'other','uid':os.getuid()}))
                target=str(root/'barrier.json') if case=='name' else str(arm)
                controller,_service,calls,start,patches=self.wiring(batch=self.BATCH)
                with self._patches(patches),patch.dict(os.environ,{worker.QA_BARRIER_ENV:target}):
                    with self.assertRaisesRegex(IndexContractError,'qa_sync_barrier_invalid'):
                        worker.run(self.project,coordinator=controller,start_service=start)
                self.assertEqual(calls,[])

    def tearDown_fixture(self):
        import shutil
        shutil.rmtree(self.project/'fixture',ignore_errors=True)

    @staticmethod
    @contextmanager
    def _patches(patches):
        with ExitStack() as stack:
            for item in patches:stack.enter_context(item)
            yield


if __name__=='__main__':unittest.main()
