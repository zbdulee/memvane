"""No ML/teacher execution: inference receipt boundaries and warm manifest reuse."""
import copy, hashlib, json, os
from pathlib import Path
import tempfile, time, unittest
from unittest.mock import patch
from context_learning import inference_identity as identity
from context_learning.dataset import read_private_json

class IdentityChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.library=self.runtime/'library.py';self.library.write_text('version=1\n')
        self.model=self.root/'model';self.model.mkdir();(self.model/'tokenizer.json').write_text('{}')
        (self.model/'config.json').write_text('{}')
        (self.model/'model.safetensors').write_bytes(b'synthetic weights')
        self.adapter=Path(identity.__file__).with_name('laya_adapter.py')
        self.argv=[os.sys.executable,str(self.adapter),str(self.model)]
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.artifact=self.state/'head';self.artifact.write_text('head');self.artifact.chmod(0o600)
        self.policy={'trainerArgv':self.argv};self.write('live-selector.json',self.policy)
        self.proof={'schema':'qmd-laya-live-proof-v1','verifiedAt':time.time(),'attestation':{'old':'approval'}}
        self.write('laya-synthetic-attestation.json',self.proof)
        self.write('active-checkpoint.json',{'artifact_path':str(self.artifact),'artifact_sha256':identity.file_sha(self.artifact)})
        self.original_scope = identity._module_scope
        self.patches=[patch.object(identity,'runtime_roots',return_value=[self.runtime]),
          patch.object(identity,'device_identity',return_value={'backend':'cpu','os':'fixture'}),
          patch.object(identity,'_module_scope',return_value=None),
          patch.object(identity,'code_identity',return_value='c'*64)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.receipt=identity._record(self.state,self.argv,view='checkpoint-config-v1',
            verified={'runtime_identity_sha256':'r'*64,'base_model_sha256':'m'*64,'adapter_sha256':identity.file_sha(self.adapter)})
        self.cache=identity.IdentityCache()
    def write(self,name,row):
        p=self.state/name;p.write_text(json.dumps(row));p.chmod(0o600)
    def validate(self):return self.cache.verify(self.state,self.argv,'checkpoint-config-v1')
    def test_warm_no_metadata_no_content_rehash(self):
        first=self.validate()
        with patch.object(identity,'file_sha',side_effect=AssertionError('warm content rehash')):
            self.assertEqual(first,self.validate())
    def test_expired_training_receipt_not_renewed(self):
        before=(self.state/'laya-synthetic-attestation.json').read_bytes()
        with patch.object(identity.time,'time',return_value=time.time()+172800):self.validate()
        self.assertEqual(before,(self.state/'laya-synthetic-attestation.json').read_bytes())
    def test_model_tokenizer_library_config_mutations(self):
        for p in [self.library,self.model/'tokenizer.json',self.model/'config.json',self.model/'model.safetensors',self.artifact]:
            self.validate();before=p.read_bytes();p.write_bytes(before+b' ')
            with self.assertRaisesRegex(ValueError,'inference_identity_changed'):self.validate()
            p.write_bytes(before)
            # Reverted bytes still require explicit re-preparation; stamps changed.
            if p != self.artifact:
                with self.assertRaises(ValueError):self.validate()
            self.setUp_record()
    def setUp_record(self):
        identity._record(self.state,self.argv,view='checkpoint-config-v1',verified={'runtime_identity_sha256':'r'*64,'base_model_sha256':'m'*64,'adapter_sha256':identity.file_sha(self.adapter)})
        self.cache=identity.IdentityCache()
    def test_added_removed_unsafe_library(self):
        self.validate();p=self.runtime/'new.py';p.write_text('x')
        with self.assertRaises(ValueError):self.validate()
        p.unlink();self.library.chmod(0o666)
        with self.assertRaises(ValueError):self.validate()
    def test_fake_torch_proxy_is_not_an_external_module_exception(self):
        import types
        fake=types.ModuleType('torch.ops');fake.__file__='_ops.py'
        with patch.dict(os.sys.modules,{'torch.ops':fake}):
            with self.assertRaisesRegex(ValueError,'inference_external_library_scope'):
                self.original_scope([Path(os.sys.base_prefix),Path(identity.__file__).resolve().parent.parent])
    def test_device_generation_code_and_scope(self):
        self.validate()
        with patch.object(identity,'device_identity',return_value={'backend':'mps','os':'fixture'}):
            with self.assertRaises(ValueError):self.validate()
        with patch.object(identity,'code_identity',return_value='d'*64):
            with self.assertRaises(ValueError):self.validate()
        with patch.object(identity,'runtime_roots',return_value=[self.model]):
            with self.assertRaises(ValueError):self.validate()
        other=self.root/'other';other.mkdir(mode=0o700)
        import shutil
        for p in self.state.iterdir():shutil.copyfile(p,other/p.name);(other/p.name).chmod(0o600)
        with self.assertRaises(ValueError):self.cache.verify(other,self.argv,'checkpoint-config-v1')
    def test_revocation_policy_change_receipt_corruption(self):
        self.validate();self.write('live-selector.json',{'trainerArgv':self.argv,'changed':True})
        with self.assertRaises(ValueError):self.validate()
        self.write('live-selector.json',self.policy);self.setUp_record();self.validate()
        (self.state/identity.RECEIPT).unlink()
        with self.assertRaises(ValueError):self.validate()
    def test_binding_deletion_cannot_downgrade(self):
        self.validate();(self.state/identity.BINDING).unlink()
        with self.assertRaises(ValueError):identity.admit(self.state,self.argv,'checkpoint-config-v1')
    def test_changed_manifest_rejected_on_warm(self):
        self.validate();p=self.state/identity.MANIFEST;p.write_text('{}')
        with self.assertRaises(ValueError):self.validate()
    def test_external_import_scope_real_guard(self):
        # Use the actual origin guard, independent of the mocked runtime roots.
        original = self.original_scope
        import types
        fake=types.ModuleType('qmd_external_fixture');fake.__file__=str(self.root/'external.py')
        with patch.dict(os.sys.modules, {'qmd_external_fixture':fake}):
            with self.assertRaisesRegex(ValueError,'inference_external_library_scope'):
                original([Path(os.sys.base_prefix),Path(identity.__file__).resolve().parent.parent])
    def test_anchor_revocation_and_unsafe_receipt(self):
        self.validate();(self.state/'laya-synthetic-attestation.json').unlink()
        with self.assertRaises(ValueError):self.validate()
        self.write('laya-synthetic-attestation.json',self.proof);self.setUp_record()
        (self.state/identity.RECEIPT).chmod(0o644)
        with self.assertRaises(ValueError):self.validate()
    def test_symlink_rejected(self):
        p=self.runtime/'alias.py';p.symlink_to(self.model/'tokenizer.json')
        with self.assertRaises(ValueError):self.validate()
    def test_both_host_layouts_code_identity_equal(self):
        # Host roots relocate identically reviewed bytes, never the runtime/model.
        with patch.object(identity,'code_identity',return_value='c'*64):self.validate()
        with self.assertRaises(ValueError):self.cache.verify(self.state,self.argv,'other-view')
    def probe(self):
        from context_learning import laya_adapter
        case={'request_id':'probe','prompt':'synthetic request','input_sha256':'0'*64,
              'candidates':[{'id':'synthetic/overflow','revision_sha256':'1'*64,
                             'input_text':'# Synthetic overflow\nbody','input_kind':'full_compact_wiki_body'}]}
        return patch.object(laya_adapter,'truncation_probe',return_value=case)
    def test_explicit_prepare_never_calls_train_or_smoke_and_rechecks_expiry(self):
        from context_learning import inference_service as service, local_cycle, runtime_setup
        clock=[100.0];expired=[False];forwards=[]
        class Trainer:
            def __init__(inner,*args,**kw):pass
            def validate_prediction(inner,**kw):return self.receipt['verified']
            def _prediction_proof(inner):
                if expired[0]:raise ValueError('trainer_inference_attestation_required')
                return self.proof
        def forward(loaded,job,**kw):
            self.assertFalse(kw['cancelled']())
            ids=[row['id'] for row in job['request']['cases'][0]['candidates']]
            forwards.append(ids[0])
            # The adapter reports the over-budget probe card it truncated.
            if ids==['synthetic/overflow']:kw['timing']['truncated_pairs']=1;expired[0]=True
        before=(self.state/identity.RECEIPT).read_bytes()
        with patch.object(local_cycle,'VerifiedLayaTrainer',Trainer), \
             patch.object(local_cycle.LocalTrainer,'call',side_effect=AssertionError('train')), \
             patch.object(runtime_setup,'attest_runtime',side_effect=AssertionError('smoke')), \
             patch.object(service,'_policy',return_value=(self.policy,self.policy)), \
             patch.object(service,'_load',return_value=object()), \
             patch.object(service,'_forward',side_effect=forward), self.probe(), \
             patch.object(service,'_rss_mib',return_value=100), \
             patch.object(service.resident,'_admission',return_value=({},{})), \
             patch.object(identity.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'trainer_inference_attestation_required'):
                identity.prepare(self.state,self.argv)
        self.assertEqual(forwards,['synthetic/cobalt','synthetic/overflow'])
        self.assertEqual(before,(self.state/identity.RECEIPT).read_bytes())
    def test_prepare_requires_the_versioned_truncation_to_run(self):
        from context_learning import inference_service as service, local_cycle
        class Trainer:
            def __init__(inner,*args,**kw):pass
            def validate_prediction(inner,**kw):return self.receipt['verified']
            def _prediction_proof(inner):return self.proof
        before=(self.state/identity.RECEIPT).read_bytes()
        with patch.object(local_cycle,'VerifiedLayaTrainer',Trainer), \
             patch.object(service,'_policy',return_value=(self.policy,self.policy)), \
             patch.object(service,'_load',return_value=object()), \
             patch.object(service,'_forward',side_effect=lambda *a,**kw:kw['timing'].update(truncated_pairs=0)), self.probe(), \
             patch.object(service,'_rss_mib',return_value=100), \
             patch.object(service.resident,'_admission',return_value=({},{})):
            with self.assertRaisesRegex(ValueError,'inference_prepare_truncation_not_exercised'):
                identity.prepare(self.state,self.argv)
        self.assertEqual(before,(self.state/identity.RECEIPT).read_bytes())
    def test_prepare_load_deadline_and_rss_guard(self):
        from context_learning import inference_service as service,local_cycle
        clock=[100.0]
        class Trainer:
            def __init__(inner,*a,**kw):pass
            def validate_prediction(inner,**kw):return self.receipt['verified']
        def load(job):clock[0]+=31;return object()
        with patch.object(local_cycle,'VerifiedLayaTrainer',Trainer), \
             patch.object(service,'_policy',return_value=(self.policy,self.policy)), \
             patch.object(service,'_load',side_effect=load), \
             patch.object(service,'_forward',side_effect=AssertionError('expired load forwarded')), \
             patch.object(service,'_rss_mib',return_value=100), \
             patch.object(service.resident,'_admission',return_value=({},{})), \
             patch.object(identity.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'inference_prepare_resource_deferred'):
                identity.prepare(self.state,self.argv)
    def test_prepare_does_not_start_load_after_scan_deadline(self):
        from context_learning import inference_service as service,local_cycle
        clock=[100.0];real_snapshot=identity.snapshot
        class Trainer:
            def __init__(inner,*a,**kw):pass
            def validate_prediction(inner,**kw):return self.receipt['verified']
        def slow_scan(*a,**kw):
            result=real_snapshot(*a,**kw);clock[0]+=31;return result
        with patch.object(local_cycle,'VerifiedLayaTrainer',Trainer), \
             patch.object(service,'_policy',return_value=(self.policy,self.policy)), \
             patch.object(service,'_load',side_effect=AssertionError('expired scan started load')), \
             patch.object(service,'_rss_mib',return_value=100), \
             patch.object(service.resident,'_admission',return_value=({},{})), \
             patch.object(identity,'snapshot',side_effect=slow_scan), \
             patch.object(identity.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'inference_prepare_resource_deferred'):
                identity.prepare(self.state,self.argv)
    def test_publication_callback_runs_before_any_write(self):
        before={p.name:p.read_bytes() for p in self.state.iterdir() if p.is_file()}
        def blocked():raise ValueError('expired_before_publication')
        with self.assertRaisesRegex(ValueError,'expired_before_publication'):
            identity._record(self.state,self.argv,view='checkpoint-config-v1',
                verified=self.receipt['verified'],before_publish=blocked)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.state.iterdir() if p.is_file()})
    def test_new_manifest_requires_new_worker_and_changes_family_identity(self):
        old=self.validate();self.library.write_text('version=2')
        identity._record(self.state,self.argv,view='checkpoint-config-v1',verified=self.receipt['verified'])
        with self.assertRaisesRegex(ValueError,'inference_identity_changed'):self.validate()
        new=identity.IdentityCache().verify(self.state,self.argv,'checkpoint-config-v1')
        self.assertNotEqual(old['runtime_identity_sha256'],new['runtime_identity_sha256'])
    def test_new_code_receipt_requires_new_worker(self):
        self.validate()
        with patch.object(identity,'code_identity',return_value='d'*64):
            identity._record(self.state,self.argv,view='checkpoint-config-v1',verified=self.receipt['verified'])
            with self.assertRaisesRegex(ValueError,'inference_identity_changed'):self.validate()
            identity.IdentityCache().verify(self.state,self.argv,'checkpoint-config-v1')
    def test_prediction_never_trains_smokes_or_prepares(self):
        from context_learning import runtime_setup,local_cycle
        with patch.object(runtime_setup,'attest_runtime',side_effect=AssertionError('smoke')), \
             patch.object(local_cycle.LocalTrainer,'call',side_effect=AssertionError('train')), \
             patch.object(identity,'prepare',side_effect=AssertionError('implicit prepare')):
            self.validate();self.validate()

if __name__=='__main__':unittest.main()
