#!/usr/bin/env python3
"""Selected worker wiring only; no model, QMD, socket or host is started."""
from contextlib import ExitStack, closing, contextmanager
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
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import prepare_candidate_batch_off_hook
from laya_index_vector import qmd_markdown_title
import laya_index_sync
import laya_vector_capacity


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


# Stage 6b: the real runtime/batch/sync_full pipeline over a disposable A-int
# DB/queue and two disjoint roots. Route proof, journal CAS, provider and
# service are synthetic; no model, QMD, socket or host is started.
SCOPE='a'*32
PROFILE={'profileId':'c'*64,'modelId':'synthetic-worker','embeddingFingerprint':'d'*64,
         'dimension':2}


class V2WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='laya-worker-v2-');self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve();self.home.chmod(0o700)
        self.alpha=self.home/'alpha';self.beta=self.home/'beta'
        for root,name,body in ((self.alpha,'one.md','silver otter'),(self.beta,'two.md','purple finch')):
            root.mkdir();(root/name).write_text(body,encoding='utf-8')
        self.index_path=self.home/'index.sqlite3';self.queue_path=self.home/'queue.sqlite3'
        with closing(LayaIndexA(self.index_path,create=True)) as index:
            self.incarnation=index.incarnation()
        ClaimQueue.create(self.queue_path,index_incarnation=self.incarnation).close()
        self.decision={'projectRoot':str(self.home),'scopeId':SCOPE,
            'dbPath':str(self.index_path),'queuePath':str(self.queue_path),
            'dbIncarnation':self.incarnation,'journalPath':str(self.home/'install-journal.sqlite'),
            'collections':[{'name':'alpha','role':'raw','sourceRoot':str(self.alpha)},
                           {'name':'beta','role':'wiki','sourceRoot':str(self.beta)}],
            'collectionRoots':{'alpha':str(self.alpha),'beta':str(self.beta)}}
        self.epoch=7;self.marks=[];self.embedded=[];self.batches=[];self.services=[]

    def proof(self):
        return {'productRouteAllowed':True,'selectionEpoch':self.epoch,
                'providerManifest':{'synthetic':True},'vectorProfile':PROFILE}

    def provider_class(self):
        test=self
        class Provider:
            profile={'profile_id':PROFILE['profileId'],'model_id':PROFILE['modelId'],
                     'embedding_fingerprint':PROFILE['embeddingFingerprint'],
                     'dimension':PROFILE['dimension']}
            manifest={'recipeId':'qmd-regex-token-chunks-candidate-v1'}
            def __init__(self,manifest):test.assertEqual(manifest,{'synthetic':True})
            def embed_document_chunks(self,body,path):
                test.embedded.append(path)
                return [{'seq':0,'pos':0,'vector':[1.0,0.0]}]
        return Provider

    def run_worker(self,decision=None):
        decision=decision or self.decision
        def verify(seen):
            self.assertIs(seen,decision)
            return self.proof()
        def cas(path,expected):
            self.assertEqual((path,expected),(decision['journalPath'],self.epoch))
            self.epoch+=1;self.marks.append(self.epoch)
            return {'epoch':self.epoch,'newWriteSeen':True}
        def start(manifest,*,admit):
            self.assertTrue(admit())
            service=Mock();self.services.append(service)
            return service
        def batch(runtime,provider):
            result=prepare_candidate_batch_off_hook(runtime,provider)
            self.batches.append(result)
            return result
        controller=Coordinator();controller.root=self.home
        patches=[patch.object(worker,'_selected',side_effect=lambda _p:(decision,self.proof())),
            patch.object(worker,'ReviewedLocalQmdProvider',self.provider_class()),
            patch.object(worker,'resource_report',return_value={'ready':True}),
            patch.object(worker,'verify_worker_attestation',return_value={'synthetic':True}),
            patch.object(worker,'verify_selected_route',side_effect=verify),
            patch.object(worker,'cas_mark_new_write',side_effect=cas),
            patch.object(worker,'prepare_candidate_batch_off_hook',side_effect=batch)]
        with WorkerChecks._patches(patches):
            result=worker.run(self.home,coordinator=controller,start_service=start)
        self.services[-1].stop.assert_called_once_with()
        return result

    def members(self,gen):
        with closing(LayaIndexA(self.index_path)) as index:
            return [row[0] for row in index.db.execute(
                'SELECT path FROM membership WHERE gen=? ORDER BY path',(gen,))]

    def claim_state(self,claim):
        with closing(ClaimQueue.open(self.queue_path)) as queue:
            return queue.get(claim)['state']

    def identities(self,batch):
        return {(chunk['hash'],chunk['title']) for chunk in batch['chunks']}

    def test_v2_two_roots_origin_then_unchanged(self):
        first=self.run_worker()
        self.assertEqual(first['state'],'indexed')
        self.assertEqual(self.marks,[8])
        self.assertEqual(self.claim_state(first['claim']),'acked')
        self.assertEqual(self.members(first['generation']),['alpha/one.md','beta/two.md'])
        self.assertEqual(sorted(self.embedded),['alpha/one.md','beta/two.md'])
        self.assertEqual(self.run_worker(),
                         {'state':'unchanged','generation':first['generation'],'claim':None})
        # No second fence or inference: sealed chunk sets are reused.
        self.assertEqual(self.marks,[8])
        self.assertEqual(sorted(self.embedded),['alpha/one.md','beta/two.md'])
        self.assertEqual(self.batches[0]['source_manifest'],self.batches[1]['source_manifest'])
        self.assertEqual(len(self.services),2)

    def test_v2_one_root_edit_publishes_full_membership_with_stable_identity(self):
        first=self.run_worker()
        (self.beta/'two.md').write_text('green heron',encoding='utf-8')
        changed=self.run_worker()
        self.assertEqual(changed['state'],'indexed')
        self.assertNotEqual(changed['generation'],first['generation'])
        self.assertEqual(self.marks,[8,9])
        self.assertEqual(self.claim_state(changed['claim']),'acked')
        self.assertEqual(self.members(changed['generation']),['alpha/one.md','beta/two.md'])
        self.assertEqual(self.members(first['generation']),['alpha/one.md','beta/two.md'])
        # Only the edited document is embedded again; the untouched root keeps
        # its namespaced (content hash, title) vector identity.
        self.assertEqual(sorted(self.embedded),['alpha/one.md','beta/two.md','beta/two.md'])
        alpha={(h,t) for h,t in self.identities(self.batches[0])
               if t==qmd_markdown_title('silver otter','alpha/one.md')}
        self.assertEqual(len(alpha),1)
        self.assertLessEqual(alpha,self.identities(self.batches[1]))
        with closing(LayaIndexA(self.index_path)) as index:
            self.assertEqual(index.db.execute('SELECT COUNT(*) FROM content').fetchone()[0],3)
        self.assertEqual(self.run_worker()['state'],'unchanged')
        self.assertEqual(self.marks,[8,9])

    def test_v2_cutover_origin_first_sync_is_unchanged_without_fence_or_inference(self):
        # Origin = installer staging (prepare_qmd_candidate over a synthetic
        # QMD-format DB) + the exact seal check/publish call of bind_qmd_origin.
        # Journal, attestation, route bytes and the cutover ledger/marker are
        # not exercised here; laya-qmd-cutover-checks.py covers those.
        import hashlib,runpy,sqlite3
        from laya_index_source_port import publish_collections_from_source,scan_collections
        from laya_index_vector import profile_identity,verify_generation_seal
        from laya_qmd_migration import (QMD_253,backup_qmd_collections,
                                        inspect_qmd_collections,prepare_qmd_candidate)
        make_source=runpy.run_path(str(Path(__file__).with_name(
            'laya-qmd-migration-checks.py')))['make_source']
        names=('alpha','beta');roots=dict(self.decision['collectionRoots'])
        files={'alpha/one.md':'silver otter','beta/two.md':'purple finch'}
        source,backup=self.home/'qmd.sqlite',self.home/'qmd-backup.sqlite'
        make_source(source,QMD_253,documents=[('alpha','one.md','silver otter',1),
                                              ('beta','two.md','purple finch',1)])
        with closing(sqlite3.connect(source)) as db,db:
            db.executemany('INSERT INTO store_collections(name,path) VALUES(?,?)',
                           sorted(roots.items()))
        backup_qmd_collections(source,QMD_253,names,backup)
        digest=inspect_qmd_collections(source,QMD_253,names)['digest']
        with closing(LayaIndexA(self.index_path)) as scratch:
            manifest=scan_collections(roots,SCOPE,scratch).manifest
        keys={path:(hashlib.sha256(body.encode('utf-8')).hexdigest(),
                    qmd_markdown_title(body,path)) for path,body in files.items()}
        profile_id=profile_identity(PROFILE['modelId'],PROFILE['embeddingFingerprint'],
                                    PROFILE['dimension'])
        candidate,queue=self.home/'candidate.sqlite',self.home/'candidate-queue.sqlite'
        gen='gen-cutover'
        prepared=prepare_qmd_candidate(backup,QMD_253,names,files,candidate,SCOPE,gen,
            migration_allowed=True,expected_collection_root=roots,
            expected_source_digest=digest,vector_batch={
                'scope':SCOPE,'source_manifest':manifest,'qmd_source_digest':digest,
                'model_id':PROFILE['modelId'],
                'embedding_fingerprint':PROFILE['embeddingFingerprint'],
                'dimension':PROFILE['dimension'],
                'chunks':[{'hash':h,'title':t,'seq':0,'pos':0,'vector':[1.0,0.0]}
                          for h,t in sorted(keys.values())]},
            provider_approved=True,current_source_db=source,scope_collections=names)
        self.assertEqual(prepared['vector_profile_id'],profile_id)
        with closing(LayaIndexA(candidate)) as index:
            index.db.execute('BEGIN')
            try:self.assertEqual(verify_generation_seal(index,gen)['profile_id'],profile_id)
            finally:index.db.rollback()
            self.assertEqual(publish_collections_from_source(
                index,roots,SCOPE,None,gen,'qmd-migration',
                claim_digest=digest,claim_epoch=0,require_vector=True),'published')
            incarnation=index.incarnation()
        ClaimQueue.create(queue,index_incarnation=incarnation).close()
        self.index_path,self.queue_path=candidate,queue
        self.decision.update(dbPath=str(candidate),queuePath=str(queue),
                             dbIncarnation=incarnation)
        def sealed():
            with closing(LayaIndexA(candidate)) as index:
                rows=set(index.db.execute('SELECT path,hash,title,profile_id '
                                          'FROM gen_vector_rows WHERE gen=?',(gen,)))
                sets=index.db.execute('SELECT COUNT(*) FROM vector_chunk_sets').fetchone()[0]
                head=index.db.execute('SELECT gen FROM head WHERE scope=?',
                                      (SCOPE,)).fetchone()[0]
            return rows,sets,head
        expected={(path,h,t,profile_id) for path,(h,t) in keys.items()}
        self.assertEqual(sealed(),(expected,2,gen))
        self.assertEqual(self.run_worker(),{'state':'unchanged','generation':gen,'claim':None})
        # No journal CAS fence and no inference: the worker reuses the sealed
        # namespaced (hash,title,profile) sets the cutover origin published.
        self.assertEqual(self.marks,[])
        self.assertEqual(self.embedded,[])
        self.assertEqual(self.members(gen),['alpha/one.md','beta/two.md'])
        self.assertEqual(self.identities(self.batches[0]),set(keys.values()))
        self.assertEqual(self.batches[0]['source_manifest'],manifest)
        self.assertEqual(sealed(),(expected,2,gen))
        with closing(ClaimQueue.open(queue)) as claims:
            self.assertEqual(claims.active_for_scope(SCOPE),[])

    def test_v1_source_root_is_passed_unchanged(self):
        decision={key:value for key,value in self.decision.items()
                  if key not in ('collections','collectionRoots')}
        decision.update(collectionName='alpha',sourceRoot=str(self.alpha))
        seen=[]
        def spy(**kwargs):
            seen.append(kwargs['source_root'])
            return LayaIndexRuntime(**kwargs)
        def refuse(*_args,**_kwargs):raise AssertionError('collection helper called for v1')
        with patch.object(worker,'LayaIndexRuntime',side_effect=spy),\
                patch.object(laya_index_sync,'scan_collections',refuse):
            first=self.run_worker(decision)
            self.assertEqual(self.run_worker(decision)['state'],'unchanged')
        self.assertEqual(first['state'],'indexed')
        self.assertIs(seen[0],decision['sourceRoot'])
        self.assertEqual(self.members(first['generation']),['one.md'])
        self.assertEqual(self.embedded,['one.md'])

    def test_malformed_v2_source_fails_before_provider_start_without_writes(self):
        (self.alpha/'nested').mkdir()
        def snapshot():
            return (sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*')),
                    self.index_path.read_bytes(),self.queue_path.read_bytes())
        before=snapshot()
        roots=self.decision['collectionRoots']
        cases={'missing':({'collectionRoots':None},'selected_collection_roots_invalid'),
            'not_mapping':({'collectionRoots':[str(self.alpha)]},'selected_collection_roots_invalid'),
            'empty':({'collectionRoots':{}},'selected_collection_roots_invalid'),
            'relative':({'collectionRoots':{**roots,'alpha':'alpha'}},'selected_collection_roots_invalid'),
            'nested':({'collectionRoots':{**roots,'beta':str(self.alpha/'nested')}},
                      'selected_collection_roots_invalid'),
            'absent_dir':({'collectionRoots':{**roots,'beta':str(self.home/'gone')}},
                          'selected_collection_roots_invalid'),
            'mismatch':({'collections':[self.decision['collections'][0]]},
                        'selected_collection_roots_mismatch'),
            'ambiguous':({'sourceRoot':str(self.alpha)},'selected_route_source_ambiguous')}
        for case,(override,reason) in cases.items():
            with self.subTest(case=case):
                decision={**self.decision,**override}
                if override.get('collectionRoots',0) is None:del decision['collectionRoots']
                controller=Coordinator();controller.root=self.home
                start=Mock()
                with patch.object(worker,'_selected',return_value=(decision,self.proof())),\
                        patch.object(worker,'ReviewedLocalQmdProvider',
                                     side_effect=AssertionError('provider')),\
                        patch.object(worker,'verify_worker_attestation',
                                     side_effect=AssertionError('attestation')),\
                        patch.dict(os.environ,{},clear=False):
                    os.environ.pop(worker.QA_BARRIER_ENV,None)
                    with self.assertRaisesRegex(IndexContractError,reason):
                        worker.run(self.home,coordinator=controller,start_service=start)
                start.assert_not_called()
                self.assertEqual(controller.lanes,[])
                self.assertEqual(snapshot(),before)


class CapacityWorkerChecks(unittest.TestCase):
    """Capacity-path selection inside run(); sync_step and the fill are fakes."""
    # Borrow the helpers only; inheriting would run WorkerChecks' tests twice.
    setUp=WorkerChecks.setUp
    wiring=WorkerChecks.wiring
    _patches=WorkerChecks.__dict__['_patches']

    def stores(self,*,catalog=True,bindings=True,mode=0o600):
        db=Path(self.decision['dbPath'])
        for want,suffix in ((catalog,worker.CATALOG_SUFFIX),(bindings,worker.BINDING_STORE_SUFFIX)):
            if want:
                path=db.with_name(db.name+suffix);path.write_text('');path.chmod(mode)

    def capacity_wiring(self,steps,fills=()):
        controller,service,calls,start,patches=self.wiring(
            sync=lambda *_a,**_k:self.fail('capacity path reached sync_full'))
        marked=[]
        def verify(_decision):
            return self.refreshed if marked else self.proof
        def cas(_journal,_epoch):
            marked.append(1);seen['cas']+=1;return {'epoch':8,'newWriteSeen':True}
        seen={'steps':[],'fills':0,'runtime':None,'scans':0,'cas':0}
        answers=iter(steps);fill_answers=iter(fills)
        def step(runtime,*,before_publish,require_vector,vector_profile_id):
            self.assertTrue(require_vector)
            self.assertEqual(seen['scans'],1)  # the wake asked for its scan first
            seen['steps'].append(vector_profile_id)
            answer=next(answers)
            if answer['state']=='indexed':
                before_publish();before_publish()  # two publishes, one fence
            return answer
        def fill(_runtime,_provider,*,should_yield):
            seen['fills']+=1;return next(fill_answers)
        def runtime(**kwargs):
            seen['runtime']=kwargs
            context=MagicMock();context.__enter__.return_value=context
            def request_scan():
                seen['scans']+=1;return {'status':'started'}
            context.request_scan.side_effect=request_scan
            return context
        patches=[p for p in patches if getattr(p,'attribute',None) not in
                 ('verify_selected_route','cas_mark_new_write','LayaIndexRuntime')]
        patches+=[patch.object(worker,'verify_selected_route',side_effect=verify),
                  patch.object(worker,'cas_mark_new_write',side_effect=cas),
                  patch.object(worker,'LayaIndexRuntime',side_effect=runtime),
                  patch.object(worker,'sync_step',side_effect=step),
                  patch('laya_vector_capacity.vector_fill_step',side_effect=fill),
                  patch('laya_vector_capacity.provider_profile_id',return_value='9'*64),
                  patch.object(worker,'prepare_candidate_batch_off_hook',
                               side_effect=lambda *_a:self.fail('capacity path built a full batch'))]
        return controller,service,start,patches,seen

    def test_both_stores_route_through_sync_step_and_fill(self):
        self.stores()
        controller,service,start,patches,seen=self.capacity_wiring(
            [{'state':'pending','reason':'vector_backlog'},{'state':'indexed','generation':'g'}],
            [{'state':'complete'}])
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,{'state':'indexed','generation':'g'})
        self.assertEqual(seen['steps'],['9'*64,'9'*64])
        self.assertEqual(seen['fills'],1)
        self.assertEqual(seen['cas'],1)  # one journal epoch per wake
        # The fake fill never embedded, so no model service was started.
        service.stop.assert_not_called()
        db=self.decision['dbPath']
        self.assertEqual((seen['runtime']['catalog_path'],seen['runtime']['binding_store_path']),
                         (db+worker.CATALOG_SUFFIX,db+worker.BINDING_STORE_SUFFIX))

    def test_rounds_are_bounded_and_pending_is_retained(self):
        self.stores()
        backlog={'state':'pending','reason':'scan_pending'}
        controller,service,start,patches,seen=self.capacity_wiring(
            [backlog]*worker.MAX_CAPACITY_ROUNDS)
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,backlog)
        self.assertEqual(len(seen['steps']),worker.MAX_CAPACITY_ROUNDS)
        with patch('laya_product_sync_worker.run',return_value=backlog):
            self.assertFalse(runtime_work.dispatch({'kind':'laya-sync','target':str(self.project)}))

    def test_one_store_without_the_other_fails_before_model_start(self):
        self.stores(bindings=False)
        controller,service,start,patches,_seen=self.capacity_wiring([])
        calls=[]
        with self._patches(patches):
            with self.assertRaisesRegex(IndexContractError,'capacity_store_incomplete'):
                worker.run(self.project,coordinator=controller,
                           start_service=lambda *a,**k:calls.append(1))
        self.assertEqual(calls,[])

    def test_qa_barrier_with_capacity_stores_fails_before_model_start(self):
        self.stores()
        controller,_service,_start,patches,_seen=self.capacity_wiring([])
        calls=[]
        with self._patches(patches),patch.object(worker,'qa_sync_barrier',return_value=object()):
            with self.assertRaisesRegex(IndexContractError,'qa_barrier_capacity_unsupported'):
                worker.run(self.project,coordinator=controller,
                           start_service=lambda *a,**k:calls.append(1))
        self.assertEqual(calls,[])

    def test_busy_catalog_defers_the_wake(self):
        self.stores()
        controller,_service,start,patches,seen=self.capacity_wiring([])
        def runtime(**_kwargs):
            context=MagicMock();context.__enter__.return_value=context
            context.request_scan.return_value={'status':'busy'}
            return context
        patches=[p for p in patches if getattr(p,'attribute',None)!='LayaIndexRuntime']
        patches.append(patch.object(worker,'LayaIndexRuntime',side_effect=runtime))
        with self._patches(patches):
            result=worker.run(self.project,coordinator=controller,start_service=start)
        self.assertEqual(result,{'state':'deferred','reason':'catalog_busy'})
        self.assertEqual(seen['steps'],[])

    def test_shared_store_mode_fails_closed(self):
        self.stores(mode=0o644)
        controller,_service,_start,patches,_seen=self.capacity_wiring([])
        with self._patches(patches):
            with self.assertRaisesRegex(IndexContractError,'capacity_store_unsafe'):
                worker.run(self.project,coordinator=controller,start_service=lambda *a,**k:None)


class V2CapacityWorkerChecks(unittest.TestCase):
    """The real runtime, catalog, binding store and sync_step behind run()."""
    setUp=V2WorkerChecks.setUp
    proof=V2WorkerChecks.proof
    provider_class=V2WorkerChecks.provider_class
    run_worker=V2WorkerChecks.run_worker
    members=V2WorkerChecks.members

    def stores(self):
        from laya_index_capacity import GenerationBindingStore
        from laya_source_catalog import LayaSourceCatalog
        roots=self.decision['collectionRoots']
        catalog=str(self.index_path)+worker.CATALOG_SUFFIX
        store=str(self.index_path)+worker.BINDING_STORE_SUFFIX
        LayaSourceCatalog(catalog,roots,SCOPE,create=True).close()
        GenerationBindingStore(store,scope=SCOPE,db_incarnation=self.incarnation,
                               roots=roots,create=True).close()
        for path in (catalog,store):os.chmod(path,0o600)

    def run_capacity(self):
        decision=self.decision
        def cas(path,expected):
            self.assertEqual((path,expected),(decision['journalPath'],self.epoch))
            self.epoch+=1;self.marks.append(self.epoch)
            return {'epoch':self.epoch,'newWriteSeen':True}
        def start(manifest,*,admit):
            self.assertTrue(admit())
            service=Mock();self.services.append(service)
            return service
        controller=Coordinator();controller.root=self.home
        before=len(self.services)
        patches=[patch.object(worker,'_selected',side_effect=lambda _p:(decision,self.proof())),
            patch.object(worker,'ReviewedLocalQmdProvider',self.provider_class()),
            patch.object(worker,'resource_report',return_value={'ready':True}),
            patch.object(worker,'verify_worker_attestation',return_value={'synthetic':True}),
            patch.object(worker,'verify_selected_route',side_effect=lambda _d:self.proof()),
            patch.object(worker,'cas_mark_new_write',side_effect=cas),
            patch.object(worker,'prepare_candidate_batch_off_hook',
                         side_effect=lambda *_a:self.fail('capacity path built a full batch'))]
        with WorkerChecks._patches(patches):
            result=worker.run(self.home,coordinator=controller,start_service=start)
        for service in self.services[before:]:
            service.stop.assert_called_once_with()
        return result,len(self.services)-before

    def test_capacity_origin_unchanged_then_one_edit(self):
        self.stores()
        first,started=self.run_capacity()
        self.assertEqual(started,1)
        self.assertEqual(first['state'],'indexed')
        self.assertEqual(self.members(first['generation']),['alpha/one.md','beta/two.md'])
        self.assertEqual(sorted(self.embedded),['alpha/one.md','beta/two.md'])
        self.assertEqual(self.marks,[8])
        with closing(LayaIndexA(self.index_path)) as index:
            from laya_index_vector import verify_generation_seal
            index.db.execute('BEGIN')
            try:
                self.assertEqual(verify_generation_seal(index,first['generation'])['profile_id'],
                                 laya_vector_capacity.provider_profile_id(
                                     self.provider_class()({'synthetic':True})))
            finally:
                index.db.commit()
        second,started=self.run_capacity()
        self.assertEqual(started,0)  # nothing to embed: no model load at all
        self.assertEqual((second['state'],second['generation']),('unchanged',first['generation']))
        self.assertEqual(len(self.embedded),2)  # cached chunk sets, no inference
        self.assertEqual(self.marks,[8])        # no second write fence
        (self.beta/'two.md').write_text('green heron',encoding='utf-8')
        # A file created between wakes is seen: the wake requests its scan.
        (self.alpha/'three.md').write_text('amber crane',encoding='utf-8')
        third,started=self.run_capacity()
        self.assertEqual(started,1)
        self.assertEqual(third['state'],'indexed')
        self.assertNotEqual(third['generation'],first['generation'])
        self.assertEqual(sorted(self.embedded[2:]),['alpha/three.md','beta/two.md'])
        self.assertEqual(self.members(third['generation']),
                         ['alpha/one.md','alpha/three.md','beta/two.md'])


if __name__=='__main__':unittest.main()
