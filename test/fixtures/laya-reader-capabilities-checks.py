"""Internal schema3 bridge: candidate proof is never production selection."""
import importlib
import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path
import time
import sqlite3
import unittest
import copy
import contextvars
import socket
import threading
import hashlib
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('candidate_reader_fixture',HERE/'laya-view-reader-checks.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
spec=importlib.util.spec_from_file_location('candidate_vector_fixture',HERE/'laya-view-vectors-checks.py')
vector_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(vector_fixture)
spec=importlib.util.spec_from_file_location('candidate_provider_fixture',HERE/'laya-embed-runtime-checks.py')
provider_fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(provider_fixture)

def candidate_snapshot(root,store,handle):
    blob=store.ingest_card(handle.worktree_id,'cards/a.md')
    raw=(root/'notes'/'src.md').read_bytes()
    fp=store.put_fingerprint(dict(worktree_id=handle.worktree_id,branch_key=handle.branch_key,
        path='notes/src.md',source_sha=hashlib.sha256(raw).hexdigest(),byte_length=len(raw),
        head_oid=handle.head_oid,tracked_state='unknown' if handle.branch_key=='non-git' else 'tracked',
        binding_state='observed',scan_id='1'*32,observed_ns=1))
    att=store.put_attestation(dict(blob_sha=blob,verification_kind='compiled',engine_identity='synthetic:codex',
        policy_digest=handle.policy_digest,receipt=dict(schema='memvane-card-evidence-v1',verdict='verified',
        result_digest='e'*64,prior_trust=None),sources=[dict(fingerprint_id=fp,spans=[dict(start=0,end=4)])]))
    return dict(scan_id='1'*32,policy_digest=handle.policy_digest,revocation_epoch=0,complete=True,
        sources=[dict(path='notes/src.md',fingerprint_id=fp,presence='present')],
        members=[dict(path='cards/a.md',blob_sha=blob,display_title='Alpha',trust_digest=att)])

class RealProviderChecks(provider_fixture.Base):
    def test_fenced_host_uses_warm_owned_provider_and_cold_lexical(self):
        manifest=self.old_manifest();profile=self.old_att['vectorProfile']
        service=provider_fixture.start_owned_embed_service(manifest,admit=lambda:True)
        self.addCleanup(service.stop)
        spec=importlib.util.spec_from_file_location('warm_upgrade_seed',HERE/'laya-reader-move-seal-checks.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        seed=module.MoveSealChecks('runTest');seed.PROVIDER_EXTRA=manifest
        self.addCleanup(seed.doCleanups)
        selected=importlib.import_module('laya_selected_project')
        fill=module.base.published.FillProvider
        with mock.patch.object(selected,'vector_profile',return_value=profile), \
             mock.patch.object(module.base.published,'vector_profile',return_value=profile), \
             mock.patch.object(fill,'embed_document_chunks',return_value=[{'seq':0,'pos':0,'vector':[1.,0.,0.,0.]}]):
            seed.setUp()
            case,reader=seed.case,seed.reader
            upgrade=importlib.import_module('laya_active_upgrade')
            host=importlib.import_module('memvane.host.host_response')
            dispatch=importlib.import_module('memvane.recall.reader_dispatch')
            new=case.home/'warm-target';new.mkdir()
            target={**case.proof,'pluginRoot':str(new),'sourceCommit':'a'*40,
                    'bundleManifestSha256':'b'*64,'ownershipManifestSha256':'f'*64}
            old=json.loads((case.state/reader.journal.PRODUCTION_ATTESTATION).read_bytes())
            new_att={**old,'sourceCommit':target['sourceCommit'],
                     'bundleManifestSha256':target['bundleManifestSha256'],
                     'ownershipManifestSha256':target['ownershipManifestSha256'],
                     'providerManifest':manifest}
            review={'proposalSha256':'9'*64,'reviewedProvider':{'vectorProfile':profile}}
            seen=[]
            actual_query=dispatch.query_endpoint
            def observe(*args,**kwargs):
                result=actual_query(*args,**kwargs);seen.append(result.semantic_status);return result
            with ExitStack() as stack:
                for patch in (seed.authority(),
                    mock.patch.object(upgrade,'review_local_provider',return_value=review),
                    mock.patch.object(upgrade,'reattest',return_value={'vectorProfile':profile}),
                    mock.patch.object(upgrade,'adopt_host_loaded_plugin',return_value=target),
                    mock.patch.object(upgrade,'inspect_full_plugin_candidate',return_value=target),
                    mock.patch.object(upgrade,'attestation_record',return_value=new_att),
                    mock.patch.object(upgrade,'validate_production_provider_attestation'),
                    mock.patch.object(upgrade,'_provider_file_identity',return_value=[[str(new/'p.py'),1,2,3,4,5]]),
                    mock.patch.object(upgrade,'_wait_for_sockets'),
                    mock.patch.object(reader.journal,'_executing_from_candidate'),
                    mock.patch.object(host,'__file__',str(new / 'memvane/host/host_response.py')),
                    mock.patch.object(dispatch,'query_endpoint',side_effect=observe),
                    mock.patch('memvane.runtime.runtime_work.request',side_effect=AssertionError('reader woke worker'))):
                    stack.enter_context(patch)
                intent=upgrade._prepare(case.project,case.state,case.journal,case.home,new,
                                        target['sourceCommit'],review['proposalSha256'],'/synthetic/model',4)
                upgrade._fence(intent)
                with mock.patch('memvane.provider.adapters.local_embed_service.start_owned_embed_service',
                                side_effect=AssertionError('reader started provider')), \
                     mock.patch.object(dispatch,'_provider_vector',wraps=dispatch._provider_vector) as embed:
                    answer=host.serve_endpoint_recall('hermes',{'cwd':str(case.project),'prompt':'otter'},cwd=str(case.project))
                    self.assertIn('otter',answer['context'])
                    self.assertEqual(seen[-1],'hybrid')
                    self.assertEqual(embed.call_count,1)
                    service.stop()
                    cold=host.serve_endpoint_recall('hermes',{'cwd':str(case.project),'prompt':'otter'},cwd=str(case.project))
                    self.assertIn('otter',cold['context'])
                    self.assertEqual(seen[-1],'lexical_only')
                    self.assertFalse(Path(manifest['serviceSocketPath']).exists())
                    # A listening peer with a mismatched provider identity
                    # cannot supply semantic results, even on the same socket.
                    peer=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                    peer.settimeout(5)
                    peer.bind(manifest['serviceSocketPath'])
                    Path(manifest['serviceSocketPath']).chmod(0o600)
                    peer.listen(1)
                    errors=[]
                    def wrong_identity():
                        try:
                            with peer.accept()[0] as conn:
                                while conn.recv(65536):pass
                                conn.sendall(json.dumps({'approvedProviderId':'wrong',
                                    'providerArtifactSha256':manifest['providerArtifactSha256'],
                                    'embeddingFingerprint':manifest['embeddingFingerprint'],
                                    'ok':True,'result':{'vector':[1.,0.,0.,0.]}}).encode())
                        except Exception as exc:
                            errors.append(str(exc))
                    thread=threading.Thread(target=wrong_identity,daemon=True);thread.start()
                    try:
                        rejected=host.serve_endpoint_recall('hermes',{'cwd':str(case.project),'prompt':'otter'},cwd=str(case.project))
                        self.assertIn('otter',rejected['context'])
                        self.assertEqual(seen[-1],'lexical_only')
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors,[])
                    finally:
                        peer.close()
                        Path(manifest['serviceSocketPath']).unlink(missing_ok=True)

    def test_real_provider_with_actual_selected_snapshot_proof(self):
        manifest=self.old_manifest();profile=self.old_att['vectorProfile']
        service=provider_fixture.start_owned_embed_service(manifest,admit=lambda:True)
        self.addCleanup(service.stop)
        candidate=vector_fixture.VectorCase('runTest');candidate.setUp();self.addCleanup(candidate.doCleanups)
        spec=importlib.util.spec_from_file_location('real_provider_selected_seed',HERE/'laya-reader-move-seal-checks.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        seed=module.MoveSealChecks('runTest');seed.PROVIDER_EXTRA=manifest
        self.addCleanup(seed.doCleanups)
        selected=importlib.import_module('laya_selected_project')
        provider_class=module.base.published.FillProvider
        with mock.patch.object(selected,'vector_profile',return_value=profile), \
                mock.patch.object(module.base.published,'vector_profile',return_value=profile), \
                mock.patch.object(provider_class,'embed_document_chunks',return_value=[{'seq':0,'pos':0,'vector':[1.0,0.0,0.0,0.0]}]):
            seed.setUp()
            opens=importlib.import_module('laya_reader_open')
            dispatch=importlib.import_module('memvane.recall.reader_dispatch')
            provider=provider_fixture.ReviewedLocalQmdProvider(manifest)
            candidate.root=seed.case.project;candidate.scope=seed.case.route['scopeId']
            wiki=seed.case.auto/'wiki'/'cards';wiki.mkdir(parents=True,exist_ok=True)
            (wiki/'a.md').write_text('# Alpha\nhello original snapshot\n',encoding='utf8')
            (candidate.root/'notes').mkdir(exist_ok=True)
            (candidate.root/'notes'/'src.md').write_text('synthetic source',encoding='utf8')
            candidate_profile=dict(model_id=profile['modelId'],model_sha=manifest['modelSha256'],
                embedding_fingerprint=profile['embeddingFingerprint'],dimension=profile['dimension'],metric='cosine',
                document_input_recipe='blob-input-v1',query_input_recipe='qmd-query-v1',
                query_template='task: search result | query: {text}')
            with mock.patch.object(candidate,'snapshot',side_effect=lambda store,handle,**_:candidate_snapshot(candidate.root,store,handle)), \
                    mock.patch.object(vector_fixture.sets,'profile',return_value=candidate_profile):
                store,repository,handle,job,recipe,profile_id=candidate.configured()
                candidate.fill(store,handle,job,recipe,profile_id,
                    embedder=lambda values,_profile:[[1.0,0.0,0.0,0.0] for _ in values])
                _generation,pin=candidate.publish(store,handle,job,recipe,profile_id);store.release(pin)
            endpoint=dispatch._issue_candidate(store._path,repository=repository,scope_id=store._scope,
                worktree_id=handle.worktree_id,checkout=candidate.root,deadline=time.monotonic()+10)
            with seed.authority(),seed.lease.resolve_endpoint(seed.case.project,action='recall',host_root=seed.case.plugin) as authority:
                decision=seed.reader.journal.select_route(str(seed.case.project),action='recall')
                with opens.inspect_warm_snapshot(authority,deadline=time.monotonic()+15) as snapshot:
                    with dispatch.approved_query_vector(authority=authority,snapshot=snapshot,decision=decision,
                            provider=provider,prompt='hello',deadline=time.monotonic()+10) as vector:
                        self.assertEqual(len(vector),manifest['dimension'])
                        result=dispatch.query_endpoint(endpoint,checkout=candidate.root,prompt='hello',query_vector=vector,
                            deadline=time.monotonic()+10,authority=authority,snapshot=snapshot,decision=decision,provider=provider)
                        self.assertEqual(result.semantic_status,'complete')
                        self.assertTrue(any(row['path']=='cards/a.md' for row in result.rows))
                    changed=provider_fixture.ReviewedLocalQmdProvider(manifest)
                    changed.manifest['modelSha256']='f'*64
                    with mock.patch.object(dispatch,'_provider_vector',side_effect=AssertionError('provider must not run')):
                        with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
                            with dispatch.approved_query_vector(authority=authority,snapshot=snapshot,decision=decision,
                                    provider=changed,prompt='hello',deadline=time.monotonic()+10):pass

    def test_provider_child_crash_is_not_treated_as_unavailable(self):
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        provider=provider_fixture.ReviewedLocalQmdProvider(self.old_manifest())
        child=mock.Mock(returncode=1)
        child.communicate.return_value=('', 'synthetic child failure')
        with mock.patch.object(dispatch,'_vector_proof',return_value={'syntheticFixture':True}), \
                mock.patch.object(dispatch.subprocess,'Popen',return_value=child):
            with self.assertRaisesRegex(ValueError,'provider_process_failed'):
                with dispatch.approved_query_vector(authority=None,snapshot=None,decision={},provider=provider,
                        prompt='synthetic query',deadline=time.monotonic()+3):pass

    def test_provider_invalid_prompt_is_not_treated_as_unavailable(self):
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        provider=provider_fixture.ReviewedLocalQmdProvider(self.old_manifest())
        with mock.patch.object(dispatch,'_vector_proof',return_value={'syntheticFixture':True}):
            with self.assertRaisesRegex(ValueError,'provider_text_invalid'):
                with dispatch.approved_query_vector(authority=None,snapshot=None,decision={},provider=provider,
                        prompt='',deadline=time.monotonic()+3):pass

    def test_provider_trickled_socket_does_not_extend_absolute_deadline(self):
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        manifest=self.old_manifest()
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        server.bind(manifest['serviceSocketPath']);server.listen(1)
        Path(manifest['serviceSocketPath']).chmod(0o600)
        def send_slowly():
            with server.accept()[0] as conn:
                while conn.recv(65536):pass
                for _ in range(20):
                    try:conn.sendall(b' ')
                    except OSError:break
                    time.sleep(.05)
        worker=threading.Thread(target=send_slowly,daemon=True);worker.start()
        provider=provider_fixture.ReviewedLocalQmdProvider(manifest)
        started=time.monotonic()
        try:
            with mock.patch.object(dispatch,'_vector_proof',return_value={'syntheticFixture':True}):
                try:
                    with dispatch.approved_query_vector(authority=None,snapshot=None,decision={},provider=provider,
                            prompt='synthetic query',deadline=started+.25):pass
                except ValueError:pass
            self.assertLess(time.monotonic()-started,.6,'socket traffic must not renew the entire deadline')
        finally:
            server.close();worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def test_approved_vector_uses_real_provider_socket_once(self):
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        manifest=self.old_manifest()
        service=provider_fixture.start_owned_embed_service(manifest,admit=lambda:True)
        self.addCleanup(service.stop)
        provider=provider_fixture.ReviewedLocalQmdProvider(manifest)
        proof={'syntheticFixture':True}
        with mock.patch.object(dispatch,'_vector_proof',return_value=proof), \
                mock.patch.object(dispatch,'_provider_vector',wraps=dispatch._provider_vector) as embed:
            with dispatch.approved_query_vector(authority=None,snapshot=None,decision={},provider=provider,
                    prompt='synthetic query',deadline=time.monotonic()+10) as vector:
                self.assertEqual(len(vector),manifest['dimension'])
                self.assertEqual(embed.call_count,1)
                self.assertGreater(embed.call_args.args[2],0)
                self.assertLessEqual(embed.call_args.args[2],2)

class VectorCapabilityChecks(vector_fixture.VectorCase):
    def test_verified_v6_snapshot_can_query_same_profile_candidate_hybrid(self):
        seed_spec=importlib.util.spec_from_file_location('candidate_selected_seed',HERE/'laya-reader-move-seal-checks.py')
        seed_module=importlib.util.module_from_spec(seed_spec);seed_spec.loader.exec_module(seed_module)
        seed=seed_module.MoveSealChecks('runTest');self.addCleanup(seed.doCleanups);seed.setUp()
        self.root=seed.case.project;self.scope=seed.case.route['scopeId']
        wiki=seed.case.auto/'wiki'/'cards';wiki.mkdir(parents=True,exist_ok=True)
        (wiki/'a.md').write_text('# Alpha\nhello original snapshot\n',encoding='utf8')
        (self.root/'notes').mkdir(exist_ok=True)
        (self.root/'notes'/'src.md').write_text('synthetic source',encoding='utf8')
        selected=seed.reader.journal.select_route(str(self.root),action='recall')
        selected_profile=selected['vectorProfile']
        candidate_profile=dict(model_id=selected_profile['modelId'],model_sha='3'*64,
            embedding_fingerprint=selected_profile['embeddingFingerprint'],dimension=selected_profile['dimension'],
            metric='cosine',document_input_recipe='blob-input-v1',query_input_recipe='qmd-query-v1',
            query_template='task: search result | query: {text}')
        def snapshot(store,handle,**_kwargs):return candidate_snapshot(self.root,store,handle)
        with mock.patch.object(self,'snapshot',side_effect=snapshot), mock.patch.object(vector_fixture.sets,'profile',return_value=candidate_profile):
            store,repository,handle,manifest,recipe,profile=self.configured()
            self.fill(store,handle,manifest,recipe,profile,embedder=lambda values,_profile:[[1.0,0.0,0.0] for _ in values])
            _generation,pin=self.publish(store,handle,manifest,recipe,profile);store.release(pin)
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        endpoint=dispatch._issue_candidate(store._path,repository=repository,scope_id=store._scope,
            worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        opens=importlib.import_module('laya_reader_open')
        actual_recheck=seed.reader._recheck
        def synthetic_manifest(token):
            record,raw,proof=actual_recheck(token)
            return record,raw,{**proof,'providerManifest':{**proof['providerManifest'],'modelSha256':'3'*64}}
        class SyntheticReviewedProvider:
            def __init__(self,manifest):
                self.manifest=manifest;self.calls=[];self.failure=None
                self.profile=dict(profile_id=selected_profile['profileId'],model_id=selected_profile['modelId'],
                    embedding_fingerprint=selected_profile['embeddingFingerprint'],dimension=selected_profile['dimension'],metric='cosine')
            def _embed(self,kind,text,*,timeout=None):
                self.calls.append((kind,text,timeout))
                if self.failure is not None:raise self.failure
                return [1.0,0.0,0.0]
        provider_module=importlib.import_module('memvane.provider.adapters.local_embed_provider')
        with seed.authority(),seed.lease.resolve_endpoint(self.root,action='recall',host_root=seed.case.plugin) as authority:
            decision=seed.reader.journal.select_route(str(self.root),action='recall')
            with opens.inspect_warm_snapshot(authority,deadline=time.monotonic()+10) as observed:
                _record,_raw,proof=synthetic_manifest(authority)
                provider=SyntheticReviewedProvider(proof['providerManifest'])
                with mock.patch.object(seed.reader,'_recheck',side_effect=synthetic_manifest), \
                        mock.patch.object(provider_module,'ReviewedLocalQmdProvider',SyntheticReviewedProvider), \
                        mock.patch.object(dispatch,'_provider_vector',side_effect=lambda obj,text,budget:obj._embed('query',text,timeout=budget)):
                    with dispatch.approved_query_vector(authority=authority,snapshot=observed,decision=decision,
                            provider=provider,prompt='hello',deadline=time.monotonic()+10) as vector:
                        result=dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=vector,
                            deadline=time.monotonic()+10,authority=authority,snapshot=observed,decision=decision,provider=provider)
                        self.assertEqual(len(provider.calls),1)
                        self.assertGreater(provider.calls[0][2],0)
                        self.assertLessEqual(provider.calls[0][2],2)
                        copied_context=contextvars.copy_context()
                    with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
                        copied_context.run(dispatch.query_endpoint,endpoint,checkout=self.root,prompt='hello',
                            query_vector=vector,deadline=time.monotonic()+10,authority=authority,
                            snapshot=observed,decision=decision,provider=provider)
                    with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
                        dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=[0.0,1.0,0.0],
                            deadline=time.monotonic()+10,authority=authority,snapshot=observed,decision=decision,provider=provider)
                    with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
                        dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=[1.0,0.0,0.0],
                            deadline=time.monotonic()+10,authority=authority,snapshot=observed,decision=decision,
                            provider=lambda *_: provider('hello',decision['vectorProfile']))
                    bad_provider=SyntheticReviewedProvider({**proof['providerManifest'],'modelSha256':'f'*64})
                    with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
                        dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=[1.0,0.0,0.0],
                            deadline=time.monotonic()+10,authority=authority,snapshot=observed,decision=decision,provider=bad_provider)
                    with dispatch.approved_query_vector(authority=authority,snapshot=observed,decision=decision,
                            provider=provider,prompt='absentlexicaltoken',deadline=time.monotonic()+10) as vector:
                        semantic=dispatch.query_endpoint(endpoint,checkout=self.root,prompt='absentlexicaltoken',
                            query_vector=vector,deadline=time.monotonic()+10,authority=authority,
                            snapshot=observed,decision=decision,provider=provider)
                    self.assertTrue(any(row['path']=='cards/a.md' for row in semantic.rows))
                    real_clock=time.monotonic;expired=[];approved_deadline=real_clock()+10
                    def clock():return approved_deadline+1 if expired else real_clock()
                    actual_search=self.Store.search_hybrid
                    def expire_after_search(store,*args,**kwargs):
                        result=actual_search(store,*args,**kwargs);expired.append(True);return result
                    with dispatch.approved_query_vector(authority=authority,snapshot=observed,decision=decision,
                            provider=provider,prompt='hello',deadline=approved_deadline) as vector:
                        with mock.patch.object(dispatch.time,'monotonic',side_effect=clock), \
                                mock.patch.object(self.Store,'search_hybrid',autospec=True,side_effect=expire_after_search):
                            with self.assertRaisesRegex(ValueError,'reader_snapshot_deadline|reader_vector_caller_unproved'):
                                dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=vector,
                                    deadline=real_clock()+30,authority=authority,snapshot=observed,decision=decision,provider=provider)
                    from memvane.kernel.errors import IndexContractError
                    provider.failure=IndexContractError('provider_unavailable')
                    with dispatch.approved_query_vector(authority=authority,snapshot=observed,decision=decision,
                            provider=provider,prompt='hello',deadline=real_clock()+10) as vector:
                        self.assertIsNone(vector)
                        lexical=dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=vector,
                            deadline=real_clock()+10,authority=authority,snapshot=observed,decision=decision,provider=provider)
                        self.assertTrue(any(row['path']=='cards/a.md' for row in lexical.rows))
                        self.assertEqual(lexical.semantic_status,'provider_absent')
                    provider.failure=RuntimeError('unexpected_fixture_error')
                    with self.assertRaisesRegex(RuntimeError,'unexpected_fixture_error'):
                        with dispatch.approved_query_vector(authority=authority,snapshot=observed,decision=decision,
                                provider=provider,prompt='hello',deadline=real_clock()+10):pass
                self.assertEqual(result.semantic_status,'complete')
                self.assertTrue(any(row['path']=='cards/a.md' for row in result.rows))

    def test_candidate_profile_requires_same_approved_provider_recipe(self):
        store,_repository,_handle,_manifest,_recipe,profile,_generation,pin=self.ready()
        store.release(pin)
        candidate=store._profile(profile)
        proof={'productRouteAllowed':True,'vectorProfile':{
            'modelId':candidate['model_id'],
            'embeddingFingerprint':candidate['embedding_fingerprint'],
            'dimension':candidate['dimension'],'metric':candidate['metric']},
            'providerManifest':{'modelSha256':candidate['model_sha']}}
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        self.assertTrue(dispatch._approved_profile_matches(candidate,proof))
        for section,key,value in (('vectorProfile','modelId','other'),
                                  ('vectorProfile','embeddingFingerprint','f'*64),
                                  ('vectorProfile','dimension',candidate['dimension']+1),
                                  ('vectorProfile','metric','dot'),
                                  ('providerManifest','modelSha256','f'*64)):
            altered=copy.deepcopy(proof);altered[section][key]=value
            self.assertFalse(dispatch._approved_profile_matches(candidate,altered),key)
        altered=copy.deepcopy(proof);altered['productRouteAllowed']=False
        self.assertFalse(dispatch._approved_profile_matches(candidate,altered))
        altered=copy.deepcopy(candidate);altered.pop('model_id')
        self.assertFalse(dispatch._approved_profile_matches(altered,proof))
        altered=copy.deepcopy(candidate);altered['query_input_recipe']='other'
        self.assertFalse(dispatch._approved_profile_matches(altered,proof))

    def test_descriptor_binds_sealed_vector_profile(self):
        store,repository,handle,_manifest,recipe,profile,generation,pin=self.ready()
        store.release(pin)
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        endpoint=dispatch._issue_candidate(store._path,repository=repository,scope_id=store._scope,
            worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        self.assertEqual(endpoint.record['generation'],generation)
        self.assertEqual(endpoint.record['vector_profile'],profile)
        self.assertEqual(endpoint.record['vector_recipe'],recipe)


class CapabilityChecks(fixture.ReaderChecks):
    def test_descriptor_rejects_changed_git_index_on_same_branch(self):
        self.git(self.root,'init','-q')
        self.git(self.root,'add','.')
        self.git(self.root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','seed')
        store,repository,handle,_=self.published()
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        endpoint=dispatch._issue_candidate(store._path,repository=repository,scope_id=store._scope,
            worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        (self.root/'notes'/'new.md').write_text('new staged source',encoding='utf8')
        self.git(self.root,'add','notes/new.md')
        with self.assertRaises(ValueError):
            dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=None,deadline=time.monotonic()+10)

    def test_candidate_descriptor_reads_bound_checkout_without_writes(self):
        store,repository,handle,generation=self.published()
        dispatch=importlib.import_module('memvane.recall.reader_dispatch')
        self.assertEqual(dispatch.reader_capabilities(),frozenset(('v6-ro-v1','schema3-ro-v1')))
        before=store._path.read_bytes()
        endpoint=dispatch._issue_candidate(store._path,repository=repository,scope_id=store._scope,
            worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        self.assertIsNone(endpoint.record['vector_profile'])
        self.assertIsNone(endpoint.record['vector_recipe'])
        result=dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=None,
                                       deadline=time.monotonic()+10)
        self.assertEqual(result.store_format,'schema3')
        self.assertEqual(result.data_generation,generation)
        self.assertTrue(any(row['path']=='cards/a.md' for row in result.rows))
        self.assertTrue(all(row['evidence']=='commitment-only' and not row['owner_verified'] for row in result.rows))
        self.assertEqual(store._path.read_bytes(),before)
        with self.assertRaises(ValueError):
            dispatch.query_endpoint(object(),checkout=self.root,prompt='hello',query_vector=None,deadline=time.monotonic()+10)
        with self.assertRaises(ValueError):
            dispatch.query_endpoint(endpoint,checkout=self.root.parent,prompt='hello',query_vector=None,deadline=time.monotonic()+10)
        with self.assertRaisesRegex(ValueError,'reader_vector_caller_unproved'):
            dispatch.query_endpoint(endpoint,checkout=self.root,prompt='hello',query_vector=[1.0,0.0],deadline=time.monotonic()+10)
        with self.assertRaises(ValueError):
            dispatch._issue_candidate(store._path,repository=repository,scope_id='f'*32,
                worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        foreign_root=self.project('foreign')
        foreign_repository=importlib.import_module('memvane.store.checkout.checkout_view').register_repository(str(foreign_root))
        with self.assertRaises(ValueError):
            dispatch._issue_candidate(store._path,repository=foreign_repository,scope_id=store._scope,
                worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        wrong_format=self.state/'unknown.sqlite'
        with sqlite3.connect(wrong_format) as db:
            db.execute('CREATE TABLE other(value TEXT)')
        wrong_format.chmod(0o600)
        original=wrong_format.read_bytes()
        with self.assertRaises(ValueError):
            dispatch._issue_candidate(wrong_format,repository=repository,scope_id=store._scope,
                worktree_id=handle.worktree_id,checkout=self.root,deadline=time.monotonic()+10)
        self.assertEqual(wrong_format.read_bytes(),original)
        spec=importlib.util.spec_from_file_location('recovery_fixture',HERE/'laya-journal-reader-recovery-checks.py')
        recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
        with recovery.fixture(converted=True) as (_helper,case,reader,_bridge,_installer,_readiness,selected):
            # The in-process candidate descriptor has no journal issuance; format
            # pairs are judged only for issued endpoints (journal-schema3 test_*).
            with self.assertRaisesRegex(ValueError,'reader_endpoint_not_issued'):
                reader.cas_reader(case.journal,expected_epoch=selected.epoch,
                    expected_endpoint=selected.preferred_endpoint,target=endpoint,transition_id='d'*32)
            self.assertEqual(reader.read_selection(case.journal).preferred_endpoint,selected.preferred_endpoint)

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        RealProviderChecks('test_real_provider_with_actual_selected_snapshot_proof'),
        RealProviderChecks('test_fenced_host_uses_warm_owned_provider_and_cold_lexical'),
        RealProviderChecks('test_provider_child_crash_is_not_treated_as_unavailable'),
        RealProviderChecks('test_provider_invalid_prompt_is_not_treated_as_unavailable'),
        RealProviderChecks('test_provider_trickled_socket_does_not_extend_absolute_deadline'),
        RealProviderChecks('test_approved_vector_uses_real_provider_socket_once'),
        VectorCapabilityChecks('test_verified_v6_snapshot_can_query_same_profile_candidate_hybrid'),
        VectorCapabilityChecks('test_candidate_profile_requires_same_approved_provider_recipe'),
        VectorCapabilityChecks('test_descriptor_binds_sealed_vector_profile'),
        CapabilityChecks('test_descriptor_rejects_changed_git_index_on_same_branch'),
        CapabilityChecks('test_candidate_descriptor_reads_bound_checkout_without_writes')]))
    raise SystemExit(not result.wasSuccessful())
