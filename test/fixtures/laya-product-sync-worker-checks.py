#!/usr/bin/env python3
"""Selected worker wiring only; no model, QMD, socket or host is started."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys
import tempfile
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

    def wiring(self,*,verify=None,sync=None,activity=None):
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
            self.assertEqual(vector_batch,{'synthetic':'complete'})
            before_publish()
            return {'state':'indexed'}
        patches=[patch.object(worker,'_selected',return_value=(self.decision,self.proof)),
            patch.object(worker,'ReviewedLocalQmdProvider',return_value=provider),
            patch.object(worker,'resource_report',return_value={'ready':True}),
            patch.object(worker,'verify_worker_attestation',return_value=self.proof['providerManifest']),
            patch.object(worker,'verify_selected_route',side_effect=verify or
                         [self.proof,self.proof,self.proof,self.proof,self.refreshed,self.refreshed]),
            patch.object(worker,'LayaIndexRuntime',return_value=context),
            patch.object(worker,'prepare_candidate_batch_off_hook',return_value={'synthetic':'complete'}),
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

    @staticmethod
    @contextmanager
    def _patches(patches):
        with ExitStack() as stack:
            for item in patches:stack.enter_context(item)
            yield


if __name__=='__main__':unittest.main()
