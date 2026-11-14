import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from tests.support.case import FixtureCase


class Calibration(FixtureCase):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(importlib.util.find_spec('memvane.runtime.bounded_calibration'),'explicit calibration policy missing')
        from memvane.runtime import bounded_calibration as calibration
        self.m=calibration;calibration.reset_test_state()
        permit=patch.object(calibration,'capability',return_value=None);permit.start();self.addCleanup(permit.stop)
        self.output=self.tmp / "output"; self.output.mkdir(mode=0o700)
        self.profile={'device':'mps','fixedMib':2897,'perPairMib':312,'modelIdentitySha256':'a'*64,
            'estimateSource':'synthetic profile','systemReserveMib':1024,'reserveSource':'synthetic reserve','maxVerifiedMicrobatchSize':1}
        self.policy={'minAvailableMib':1024,'maxRssMib':3584,'maxLoadPerCpu':.8,'adaptiveMemory':self.profile}
        self.request={'schema_version':1,'artifact_path':str(self.output/'head.artifact'),
            'model_input_view':'tool-edit-8192-choice-v1','train':[{
                'request_id':'synthetic-one-step-cobalt','input_sha256':'b'*64,
                'prompt':'What is the synthetic Cobalt beacon code?',
                'candidates':[{'id':'cobalt','input_kind':'full_compact_wiki_body','input_text':'The synthetic Cobalt beacon code is COBALT-271.'},
                              {'id':'garden','input_kind':'full_compact_wiki_body','input_text':'The synthetic garden marker is GARDEN-909.'}],
                'selected_ids':['cobalt']}]}
        self.samples=[self.row(t) for t in (10.,13.,16.)]
        self.envelope=calibration.create(self.samples,self.policy,self.request,command=['node','--test','/synthetic/one.test.mjs'],files={},now=16.)
        self.scoped={**self.policy,'calibration':self.envelope}


    def row(self,t,**kw):
        return {'observed_at_mono':t,'ready':True,'available_mib':4506,'total_mib':24576,
            'load_per_cpu':.2,'swap_used_mib':1000.,'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'warning','system_free_percent':38},**kw}


    def test_ordinary_warning_opens_only_with_free_memory(self):
        self.assertTrue(self.m.pressure_ready(self.row(17),self.policy,now=17.))
        scarce=self.row(17,memory_pressure={'level':'warning','system_free_percent':9})
        self.assertFalse(self.m.pressure_ready(scarce,self.policy,now=17.))


    def test_explicit_stable_warning_can_fit_below_ratio(self):
        self.assertTrue(self.m.pressure_ready(self.row(17),self.scoped,now=17.))


    def test_critical_unknown_expired_or_small_reserve_refuse(self):
        for row in (self.row(17,memory_pressure={'level':'critical','system_free_percent':2}),
                    self.row(17,memory_pressure=None),self.row(17,available_mib=1023),
                    self.row(17,swap_used_mib=None)):
            self.m.reset_test_state()
            self.assertFalse(self.m.pressure_ready(row,self.scoped,now=17.))
        self.m.reset_test_state()
        self.assertFalse(self.m.pressure_ready(self.row(47),self.scoped,now=47.))


    def test_admission_needs_absolute_task_plus_reserve_and_stable_window(self):
        for samples in ([self.row(t,available_mib=4232) for t in (10.,13.,16.)],
                        [self.row(t) for t in (14.,15.,16.)],
                        [self.row(10),self.row(13,swap_used_mib=1001),self.row(16)]):
            with self.assertRaises(ValueError):self.m.create(samples,self.policy,self.request,command=[],files={},now=16.)


    def test_running_uses_system_reserve_not_another_cold_envelope(self):
        self.assertTrue(self.m.pressure_ready(self.row(17,available_mib=1500),self.scoped,now=17.))


    def test_swap_total_growth_and_interval_rate_are_both_vetoes(self):
        self.assertTrue(self.m.pressure_ready(self.row(17,swap_used_mib=1000),self.scoped,now=17.))
        self.assertFalse(self.m.pressure_ready(self.row(18,swap_used_mib=1009),self.scoped,now=18.))


    def test_swap_baseline_is_not_reset_after_decrease(self):
        self.assertTrue(self.m.pressure_ready(self.row(17,swap_used_mib=900),self.scoped,now=17.))
        self.assertFalse(self.m.pressure_ready(self.row(27,swap_used_mib=1064),self.scoped,now=27.))


    def test_pressure_deterioration_after_improvement_stops(self):
        self.assertTrue(self.m.pressure_ready(self.row(17,memory_pressure={'level':'normal','system_free_percent':40}),self.scoped,now=17.))
        self.assertFalse(self.m.pressure_ready(self.row(18),self.scoped,now=18.))


    def test_changed_policy_request_or_model_cannot_reuse_permission(self):
        self.assertFalse(self.m.pressure_ready(self.row(17),{**self.scoped,'maxRssMib':4096},now=17.))
        self.m.validate_request({**self.request,'learning_control':{'policy':self.scoped}},now=17.)
        with self.assertRaises(ValueError):self.m.validate_request({**self.request,'train':self.request['train']*2,'learning_control':{'policy':self.scoped}},now=17.)


    def test_scope_cannot_expand_batch_or_payload(self):
        for change in ({'maxVerifiedMicrobatchSize':2},{'fixedMib':2000}):
            with self.assertRaises(ValueError):self.m.create(self.samples,{**self.policy,'adaptiveMemory':{**self.profile,**change}},self.request,command=[],files={},now=16.)


    def test_private_envelope_binds_exact_command_and_runner_files(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();sources=[]
            for n,name in enumerate(('one-step.test.mjs','one-step.py','observed-current-adapter.py')):
                p=root/name;p.write_text(f'fixed synthetic runner {n}');p.chmod(0o600);sources.append(p)
            files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
            command=['node','--test',str(sources[0])]
            value=self.m.create(self.samples,self.policy,self.request,command=command,files=files,now=16.)
            p=root/'envelope.json';p.write_text(json.dumps(value));p.chmod(0o600)
            with patch.object(self.m.time,'monotonic',return_value=17.):
                self.assertEqual(self.m.load(p,command)['runId'],value['runId'])
                with self.assertRaises(ValueError):self.m.load(p,command+['extra'])
                with self.assertRaises(ValueError):self.m.load(p,['node','--test','/other/unpinned.test.mjs'])
                sources[1].write_text('changed source')
                with self.assertRaises(ValueError):self.m.load(p,command)


    def test_bad_sample_latches_failure_for_same_run(self):
        self.assertFalse(self.m.pressure_ready(self.row(17,available_mib=900),self.scoped,now=17.))
        self.assertFalse(self.m.pressure_ready(self.row(18),self.scoped,now=18.))


    def test_output_scope_refuses_symlinks_existing_bytes_and_nonprivate_parent(self):
        self.output.chmod(0o755)
        with self.assertRaises(ValueError):self.m.validate_request({**self.request,'learning_control':{'policy':self.scoped}},now=17.)
        self.output.chmod(0o700)
        artifact=Path(self.request['artifact_path']);artifact.write_text('preserve existing bytes')
        with self.assertRaises(ValueError):self.m._output(self.request,new=True)
        self.assertEqual(artifact.read_text(),'preserve existing bytes')
