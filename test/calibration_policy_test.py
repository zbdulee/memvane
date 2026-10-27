"""One-shot calibration contracts: fake observations/private files, no models."""
import fcntl,hashlib,importlib.util,json,os,sys,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))

class Calibration(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('context_learning.bounded_calibration'),'explicit calibration policy missing')
        from context_learning import bounded_calibration as calibration
        self.m=calibration;calibration.reset_test_state()
        permit=patch.object(calibration,'capability',return_value=None);permit.start();self.addCleanup(permit.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name).resolve();self.output.chmod(0o700)
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

    def test_common_admission_and_watch_consume_the_same_scope(self):
        from context_learning.resource_coordination import decision
        from context_learning.training_watch import Watch
        row=decision(self.scoped,self.row(17),now=17.)
        self.assertTrue(row['ready'],row)
        watch=Watch(clock=lambda:17.,min_available_mib=1024,policy=self.scoped)
        self.assertEqual(watch.observe(self.row(17))['action'],'continue')
        self.assertEqual(Watch(clock=lambda:17.,min_available_mib=1024).observe(self.row(17))['action'],'yield')

    def test_outer_guard_keeps_ordinary_warning_and_budgets(self):
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('guard_calibration',Path(__file__).resolve().parents[1]/'scripts/development_guard.py')
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        system={'total_bytes':24576*1024**2,'available_percent':20,'swap_bytes':1000*1024**2,
                'load_per_cpu':.2,'cpu_count':18,'pressure':'warning'}
        self.assertEqual(guard.violation(guard.Policy(),system,[],system['swap_bytes']),'os_memory_pressure')
        with patch.object(self.m.time,'monotonic',return_value=17.):
            self.assertIsNone(guard.violation(guard.Policy(),system,[],system['swap_bytes'],calibration_policy=self.scoped,report=self.row(17)))
            self.assertEqual(guard.violation(guard.Policy(),system,[{'rss_bytes':4*1024**3,'cpu_percent':1}],system['swap_bytes'],calibration_policy=self.scoped,report=self.row(17)),'tree_rss')

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

    def test_calibration_permission_is_not_an_installed_policy_field(self):
        from context_learning import shared
        from context_learning.local_cycle import _atomic_json
        keys=('goldModel','maxGoldCalls','batchMin','batchMax','targetExact','trainerArgv',
              'baseArtifact','maxRssMib','minAvailableMib','maxLoadPerCpu','safetySeconds','experimental')
        value={k:None for k in keys}
        value.update(schema=shared.SCHEMA,enabled=True,calibration=self.envelope)
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();root.chmod(0o700)
            _atomic_json(root/shared.POLICY_NAME,value)
            with self.assertRaisesRegex(ValueError,'invalid_shared_policy'):shared._policy(root)

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


class GuardCapability(unittest.TestCase):
    def setUp(self):
        from context_learning import bounded_calibration as calibration
        self.m=calibration
        # Use the policy test's builder without its capability mock.
        fixture=Calibration();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.value=fixture.envelope;self.policy=fixture.scoped;self.request=fixture.request
        fixture.doCleanups()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700);(self.root/'calibrations').mkdir(mode=0o700)
        self.value['request']['artifact_path']=str(self.root/'out/head.artifact')
        (self.root/'out').mkdir(mode=0o700)
        self.value['artifactPath']=self.value['request']['artifact_path'];self.value['requestSha256']=self.m._sha(self.value['request'])
        self.path=self.root/'envelope.json';self.write(self.path,self.value)
        self.claim=self.root/'calibrations'/(self.value['runId']+'.json')
        self.write(self.claim,{'run':'owned-run','nonce':'owned-nonce','envelopePath':str(self.path),'envelopeSha256':self.m._sha(self.value)})
        self.write(self.root/'active.json',{'run':'owned-run','calibrationRunId':self.value['runId'],'calibrationNonce':'owned-nonce','settled':False,'status':'running'})
        self.fd=os.open(self.root/'calibrations'/(self.value['runId']+'.lock'),os.O_CREAT|os.O_RDWR,0o600)
        self.addCleanup(lambda:os.close(self.fd));fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for stub in (patch.object(self.m,'claim_root',return_value=self.root),patch.object(self.m.time,'monotonic',return_value=17.),patch.dict(os.environ,{'QMD_CALIBRATION_ENVELOPE':str(self.path)})):
            stub.start();self.addCleanup(stub.stop)

    @staticmethod
    def write(path,value):path.write_text(json.dumps(value));path.chmod(0o600)

    def test_live_matching_guard_is_required(self):
        self.m.capability(self.policy)
        fcntl.flock(self.fd,fcntl.LOCK_UN)
        with self.assertRaises(ValueError):self.m.capability(self.policy)

    def test_embedded_permission_or_finished_guard_is_insufficient(self):
        with patch.dict(os.environ,{'QMD_CALIBRATION_ENVELOPE':''}):
            with self.assertRaises(ValueError):self.m.capability(self.policy)
        self.write(self.root/'active.json',{'status':'passed','settled':True})
        with self.assertRaises(ValueError):self.m.capability(self.policy)

    def test_same_scope_cannot_launch_a_second_model(self):
        request={**self.value['request'],'learning_control':{'policy':self.policy}}
        self.m.reserve_launch(request)
        with self.assertRaisesRegex(ValueError,'already_launched'):self.m.reserve_launch(request)

    def test_guard_establishes_and_retires_capability_and_refuses_replay(self):
        import types
        from unittest.mock import Mock
        spec=importlib.util.spec_from_file_location('guard_live_capability',Path(__file__).resolve().parents[1]/'scripts/development_guard.py')
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        runtime=self.root/'guard';runtime.mkdir(mode=0o700)
        files={}
        for name in ('one-step.test.mjs','one-step.py','observed-current-adapter.py'):
            p=self.root/name;p.write_text('fixed synthetic source');p.chmod(0o600);files[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        command=['node','--test','--test-concurrency=1',str(self.root/'one-step.test.mjs')]
        value={**self.value,'command':command,'files':files}
        self.write(self.path,value)
        policy={**self.policy,'calibration':value}
        row={'ready':True,'observed_at_mono':17.,'available_mib':4506,'total_mib':24576,
            'load_per_cpu':.2,'swap_used_mib':1000.,'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'warning','system_free_percent':38}}
        system={'total_bytes':24576*1024**2,'available_percent':38,'swap_bytes':1000*1024**2,'load_per_cpu':.2,'cpu_count':18,'pressure':'warning'}
        child=Mock(pid=888888);child.poll.return_value=0
        tree=Mock();tree.observe.return_value={};tree.established=True
        def spawn(*a,**kw):self.m.capability(policy);return child
        args=types.SimpleNamespace(command=command,kind='test',max_seconds=30,sample_seconds=2,calibration_envelope=str(self.path))
        with patch.object(self.m,'claim_root',return_value=runtime),patch.object(guard,'execution_hold',return_value=False), \
             patch.object(guard,'system_sample',return_value=system),patch.object(guard,'processes',return_value=[]), \
             patch.object(guard,'OwnedTree',return_value=tree),patch('context_learning.shared.resource_report',return_value=row), \
             patch.object(guard.subprocess,'Popen',side_effect=spawn) as launch:
            self.assertEqual(guard.run_job(args,runtime),0)
            with self.assertRaises(ValueError):self.m.capability(policy)
            self.assertEqual(guard.run_job(args,runtime),75)
            self.assertEqual(launch.call_count,1)

if __name__=='__main__':unittest.main()
