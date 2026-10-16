"""Synthetic platform/resource-policy checks; no OS probes, ML or child jobs."""
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning import memory_policy
from context_learning.local_cycle import ResourceDeferred
from context_learning.resource_coordination import Coordinator,decision


class MemoryBudget(unittest.TestCase):
    def setUp(self):
        self.profile={'device':'mps','fixedMib':2897,'perPairMib':312,
            'estimateSource':'historical short synthetic MPS envelope',
            'modelIdentitySha256':'a'*64,'systemReserveMib':1024,
            'reserveSource':'synthetic fixture only, not a host recommendation',
            'uncertainty':'old adapter and narrow input shape',
            'maxVerifiedMicrobatchSize':1}
        self.policy={'minAvailableMib':768,'maxRssMib':3584,'maxLoadPerCpu':1,
                     'adaptiveMemory':self.profile}
        self.report={'ready':True,'observed_at_mono':10.,'total_mib':24576,
            'available_mib':6436,'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'normal','system_free_percent':53}}

    def test_below_35_percent_can_fit_task_plus_explicit_reserve(self):
        row=decision(self.policy,self.report,now=10.)
        self.assertTrue(row['ready'],row)
        budget=row['memory_admission']
        self.assertEqual(budget['required_additional_mib'],3209)
        self.assertEqual(budget['reserve_mib'],1024)
        self.assertFalse(budget['target_met'])
        self.assertEqual(row['microbatch_size'],1)

    def test_small_ram_above_35_percent_cannot_fit_insufficient_task_memory(self):
        row=decision(self.policy,{**self.report,'total_mib':4096,'available_mib':2200},now=10.)
        self.assertFalse(row['ready'])

    def test_resident_credit_never_spends_system_reserve(self):
        row=decision(self.policy,{**self.report,'available_mib':900},resident_credit=9999,now=10.)
        self.assertFalse(row['ready'])

    def test_large_ram_does_not_turn_ratio_into_a_required_absolute_floor(self):
        row=decision(self.policy,{**self.report,'total_mib':131072},now=10.)
        self.assertTrue(row['ready'],row)

    def test_legacy_upper_bound_proxy_is_labeled_and_keeps_explicit_floor(self):
        policy={k:v for k,v in self.policy.items() if k!='adaptiveMemory'}
        row=decision(policy,self.report,now=10.)
        self.assertTrue(row['ready'],row)
        self.assertEqual(row['memory_admission']['estimate_source'],'configured_max_rss_proxy')
        self.assertFalse(decision(policy,{**self.report,'available_mib':4000},now=10.)['ready'])

    def test_loaded_self_credit_is_not_reused_by_a_new_resume_process(self):
        report={**self.report,'available_mib':2300}
        warm=memory_policy.assess(report,self.policy,resident_credit=2000,phase='loaded_self')
        self.assertTrue(warm['ready'],warm)
        self.assertEqual(warm['required_additional_mib'],1209)
        cold=memory_policy.assess(report,self.policy,resident_credit=2000,phase='resume_new_process')
        self.assertFalse(cold['ready'])
        self.assertEqual(cold['required_additional_mib'],3209)

    def test_explicit_warm_increment_avoids_cold_transient_re_reservation(self):
        profile={**self.profile,'warmAdditionalMib':200,'warmEstimateSource':'synthetic owned-process warm measurement'}
        row=memory_policy.assess({**self.report,'available_mib':1500},
            {**self.policy,'adaptiveMemory':profile},phase='loaded_self',resident_credit=2000)
        self.assertTrue(row['ready'],row)
        self.assertEqual(row['required_additional_mib'],200)

    def test_warm_estimate_requires_measured_owned_allocation(self):
        profile={**self.profile,'warmAdditionalMib':200,'warmEstimateSource':'synthetic warm measurement'}
        row=memory_policy.assess({**self.report,'available_mib':1500},
            {**self.policy,'adaptiveMemory':profile},phase='loaded_self')
        self.assertFalse(row['ready'],row)

    def test_malformed_policy_and_credit_fail_closed(self):
        for override in ({'minAvailableMib':None},{'maxRssMib':None},{'maxLoadPerCpu':None}):
            self.assertFalse(decision({**self.policy,**override},self.report,now=10.)['ready'])
        for credit in (None,'unknown',True):
            self.assertFalse(decision(self.policy,self.report,resident_credit=credit,now=10.)['ready'])

    def test_linux_cpu_admission_uses_psi_and_keeps_mps_profile_incompatible(self):
        report={**self.report,'platform':{'os':'linux'},
                'memory_pressure':{'level':'normal','psi_full_avg10':0.}}
        self.assertFalse(decision(self.policy,report,now=10.)['ready'])
        policy={**self.policy,'adaptiveMemory':{**self.profile,'device':'cpu'}}
        self.assertTrue(decision(policy,report,now=10.)['ready'])
        self.assertFalse(decision(policy,{**report,'memory_pressure':{'level':'normal'}},now=10.)['ready'])

    def test_mps_observed_topology_requires_supported_host_unified_memory(self):
        supported={'os':'darwin','scope':'host','support':'partial','memory_model':'unified'}
        self.assertTrue(decision(self.policy,{**self.report,'platform':supported},now=10.)['ready'])
        for observed in (None,{},
                {**supported,'memory_model':'unknown'},
                {**supported,'support':'unsupported'},
                {**supported,'scope':'guest'},
                {**supported,'os':'win32'}):
            row=decision(self.policy,{**self.report,'platform':observed},now=10.)
            self.assertFalse(row['ready'],row)
            self.assertEqual(row['reason'],'memory_backend_unsupported')
        cpu={**self.policy,'adaptiveMemory':{**self.profile,'device':'cpu'}}
        self.assertTrue(decision(cpu,{**self.report,'platform':{**supported,'memory_model':'unknown'}},now=10.)['ready'])

    def test_running_watch_invalid_reserve_is_unknown_and_psi_stays_distinct(self):
        from context_learning.training_watch import Watch
        for reserve in (None,'unknown',float('nan')):
            report={**self.report,'memory_admission':{'reserve_mib':reserve}}
            self.assertEqual(Watch(clock=lambda:10.).observe(report)['action'],'kill')
        report={**self.report,'memory_pressure':{'level':'normal','psi_full_avg10':0.}}
        self.assertEqual(Watch(clock=lambda:10.).observe(report)['action'],'continue')
        self.assertEqual(Watch(clock=lambda:10.).observe({**report,'memory_pressure':{'level':'normal'}})['action'],'kill')

    def test_expansion_requires_verified_shape_and_post_allocation_target(self):
        profile={**self.profile,'maxVerifiedMicrobatchSize':2}
        policy={**self.policy,'adaptiveMemory':profile}
        row=memory_policy.assess({**self.report,'available_mib':12000},policy)
        self.assertEqual(row['training_limits']['microbatchSize'],1)
        row=memory_policy.assess({**self.report,'available_mib':13000},policy)
        self.assertEqual(row['training_limits']['microbatchSize'],2)
        legacy={k:v for k,v in profile.items() if k!='maxVerifiedMicrobatchSize'}
        row=memory_policy.assess({**self.report,'available_mib':13000},{**policy,'adaptiveMemory':legacy})
        self.assertEqual(row['training_limits']['microbatchSize'],1)

    def test_unknown_or_unsourced_new_reserve_and_warm_estimate_refuse(self):
        for override in ({'reserveSource':''},{'systemReserveMib':None},
                         {'warmAdditionalMib':200},{'maxVerifiedMicrobatchSize':3}):
            row=memory_policy.assess(self.report,{**self.policy,'adaptiveMemory':{**self.profile,**override}})
            self.assertFalse(row['ready'])

    def test_pressure_veto_is_independent_of_fitting_memory(self):
        for pressure in (None,{'level':'warning','system_free_percent':53}):
            row=decision(self.policy,{**self.report,'memory_pressure':pressure},now=10.)
            self.assertFalse(row['ready'])

    def test_recovery_does_not_add_an_unrelated_40_percent_ram_floor(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();root.chmod(0o700);clock=[10.]
            c=Coordinator(root,clock=lambda:clock[0],wall=lambda:100+clock[0])
            def attempt(available):
                with c.step(self.policy,{**self.report,'available_mib':available,'observed_at_mono':clock[0]}):pass
            with self.assertRaises(ResourceDeferred):attempt(3000)
            clock[0]=11.
            with self.assertRaises(ResourceDeferred):attempt(6436)
            clock[0]=16.;attempt(6436)

    def test_running_watch_does_not_force_yield_below_soft_ratio_target(self):
        from context_learning.training_watch import Watch
        watch=Watch(clock=lambda:10.,min_available_mib=1024)
        self.assertEqual(watch.observe({**self.report,'total_mib':131072})['action'],'continue')
        self.assertEqual(watch.observe({**self.report,'available_mib':900})['action'],'yield')


class FakeFile:
    def __init__(self,name,files):self.name=str(name);self.files=files
    def is_file(self):return self.name in self.files
    def read_text(self):return self.files[self.name]


class PlatformObservation(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('resource_platform'),'platform adapters missing')
        import resource_platform
        return resource_platform

    def test_apple_silicon_capabilities_keep_gpu_and_thermal_unknown(self):
        adapter=self.module().adapter_for('darwin',machine='arm64',release='synthetic')
        row=adapter.describe()
        self.assertEqual(row['memory_model'],'unified')
        self.assertEqual(row['gpu_utilization'],'unknown')
        self.assertEqual(row['thermal'],'unknown')
        self.assertFalse(row['hard_cpu_quota'] or row['hard_gpu_compute_quota'])

    def test_intel_mac_does_not_assume_unified_gpu_memory(self):
        row=self.module().adapter_for('darwin',machine='x86_64',release='synthetic').describe()
        self.assertEqual(row['memory_model'],'unknown')

    def test_windows_is_explicitly_unsupported_without_synthetic_zeros(self):
        adapter=self.module().adapter_for('win32',machine='AMD64',release='synthetic')
        row=adapter.observe(run=Mock(side_effect=AssertionError('OS probe')),clock=lambda:0.)
        self.assertIsNone(row['available_mib']);self.assertIsNone(row['gpu_usage_percent'])
        self.assertEqual(row['platform']['support'],'unsupported')

    def test_linux_and_wsl_preserve_memavailable_and_psi_but_not_host_visibility(self):
        module=self.module()
        files={'/proc/meminfo':'MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\nSwapTotal: 1024 kB\nSwapFree: 1024 kB\n',
               '/proc/pressure/memory':'some avg10=0.00\nfull avg10=0.00 avg60=0.00\n'}
        for release,scope in [('synthetic-linux','host'),('synthetic-microsoft-WSL2','guest')]:
            adapter=module.adapter_for('linux',machine='x86_64',release=release)
            row=adapter.observe(path_factory=lambda name:FakeFile(name,files),
                run=Mock(side_effect=AssertionError('subprocess probe')),clock=lambda:0.,
                cpu_count=lambda:4,loadavg=lambda:(.1,0,0),detailed=True)
            self.assertEqual(row['available_mib'],4096)
            self.assertTrue(module.normal_pressure(row))
            self.assertNotIn('system_free_percent',row['memory_pressure'])
            self.assertEqual(row['platform']['scope'],scope)
            self.assertIsNone(row['gpu_usage_percent'])

    def test_mac_queries_share_one_deadline_and_unknown_is_not_zero(self):
        adapter=self.module().adapter_for('darwin',machine='arm64',release='synthetic')
        clock=[0.];calls=[]
        def run(argv,**options):
            import subprocess
            calls.append(argv);clock[0]+=options['timeout'];raise subprocess.TimeoutExpired(argv,options['timeout'])
        row=adapter.observe(run=run,clock=lambda:clock[0],deadline=.3,
            path_factory=lambda name:FakeFile(name,{}),cpu_count=lambda:18,loadavg=lambda:(.1,0,0))
        self.assertEqual(len(calls),1)
        self.assertIsNone(row['available_mib']);self.assertIsNone(row['gpu_usage_percent'])

    def test_common_wrapper_preserves_linux_psi_and_adaptive_cpu_policy(self):
        from context_learning import shared
        module=self.module()
        files={'/proc/meminfo':'MemTotal: 8388608 kB\nMemAvailable: 6291456 kB\n',
               '/proc/pressure/memory':'full avg10=0.00 avg60=0.00\n'}
        policy={'minAvailableMib':768,'maxRssMib':3584,'maxLoadPerCpu':1,
            'adaptiveMemory':{'device':'cpu','fixedMib':2897,'perPairMib':312,
                'estimateSource':'synthetic fixture','modelIdentitySha256':'a'*64,
                'systemReserveMib':1024,'reserveSource':'synthetic reserve'}}
        with patch.object(module,'adapter_for',return_value=module.adapter_for('linux',machine='x86_64',release='synthetic')), \
             patch.object(shared,'Path',side_effect=lambda name:FakeFile(name,files)), \
             patch.object(shared.subprocess,'run',side_effect=AssertionError('OS probe')), \
             patch.object(shared.os,'cpu_count',return_value=18), \
             patch.object(shared.os,'getloadavg',return_value=(.1,0,0)), \
             patch.object(shared.time,'monotonic',return_value=10.):
            row=shared.resource_report(policy)
        self.assertTrue(row['ready'],row)
        self.assertEqual(row['memory_admission']['required_additional_mib'],3209)
        self.assertNotIn('system_free_percent',row['memory_pressure'])

    def test_common_wrapper_does_not_admit_unsupported_windows_observations(self):
        from context_learning import shared
        module=self.module()
        with patch.object(module,'adapter_for',return_value=module.adapter_for('win32',machine='AMD64',release='synthetic')), \
             patch.object(shared.subprocess,'run',side_effect=AssertionError('OS probe')), \
             patch.object(shared.time,'monotonic',return_value=10.):
            row=shared.resource_report({'minAvailableMib':768,'maxRssMib':3584,'maxLoadPerCpu':1})
        self.assertFalse(row['ready'])
        self.assertIsNone(row['load_per_cpu'])
        self.assertEqual(row['platform']['support'],'unsupported')

    def test_mps_controls_are_memory_checks_not_utilization_or_gpu_quotas(self):
        self.assertIsNotNone(importlib.util.find_spec('resource_controls'),'control adapters missing')
        from resource_controls import MpsMemoryControl
        torch=Mock();torch.mps.driver_allocated_memory.return_value=901
        torch.mps.recommended_max_memory.return_value=1000
        with self.assertRaisesRegex(ValueError,'training_memory_budget'):MpsMemoryControl.check(torch,{'reserveFraction':.1})
        torch.mps.recommended_max_memory.return_value=0
        with self.assertRaisesRegex(ValueError,'training_memory_unavailable'):MpsMemoryControl.check(torch,{'reserveFraction':.1})
        torch.mps.set_per_process_memory_fraction.assert_not_called()


if __name__=='__main__':unittest.main()
