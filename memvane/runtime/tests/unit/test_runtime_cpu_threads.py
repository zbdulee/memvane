import importlib.util
import os
import time
from unittest.mock import Mock, patch
from tests.support.case import FixtureCase


class CpuThreads(FixtureCase):
    def setUp(self):
        super().setUp()
        topology=patch("os.cpu_count",return_value=18);topology.start();self.addCleanup(topology.stop)
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)

    def cpu(self):
        self.assertIsNotNone(importlib.util.find_spec('memvane.runtime.runtime_cpu'),'CPU policy missing')
        from memvane.runtime import runtime_cpu
        return runtime_cpu


    def test_shares_are_derived_from_common_aggregate_fraction(self):
        cpu=self.cpu()
        self.assertEqual(cpu.plan(cpu_count=18)['capacity'],4)
        self.assertEqual(cpu.plan(cpu_count=4)['capacity'],1)
        self.assertEqual(cpu.MAX_CPU_FRACTION,.25)
        for count in (None,0,True,3):
            with patch.object(cpu.os,'cpu_count',return_value=count):
                with self.assertRaises(ValueError):cpu.plan()


    @patch.dict(os.environ, {"QMD_SETUP_GUARD_FIXTURE": ""})
    def test_overlapping_children_cannot_reserve_same_aggregate_share(self):
        cpu=self.cpu()
        with patch.object(cpu.os,'cpu_count',return_value=4):
            with cpu.lease(self.root) as first:
                self.assertEqual(first.threads,1)
                with self.assertRaises(ValueError):
                    with cpu.lease(self.root):self.fail('oversubscribed')
            with cpu.lease(self.root):pass


    @patch.dict(os.environ, {"QMD_SETUP_GUARD_FIXTURE": ""})
    def test_inherited_descriptor_retains_share_after_parent_context(self):
        cpu=self.cpu();held=None
        with patch.object(cpu.os,'cpu_count',return_value=4):
            try:
                with cpu.lease(self.root) as budget:held=os.dup(budget.fds[0])
                with self.assertRaises(ValueError):
                    with cpu.lease(self.root):self.fail('child share lost')
            finally:
                if held is not None:os.close(held)
            with cpu.lease(self.root):pass


    def test_fixture_mode_does_not_open_the_real_coordination_root(self):
        cpu=self.cpu()
        with patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'1'}),patch('memvane.runtime.resource_coordination.Coordinator',side_effect=AssertionError('fixture touched live root')):
            with cpu.lease(self.root) as budget:self.assertEqual(budget.fds,())


    def test_environment_overrides_unsafe_parent_parallelism(self):
        cpu=self.cpu();env=cpu.environment({'OMP_NUM_THREADS':'99','TOKENIZERS_PARALLELISM':'true','INDEX_PATH':'synthetic'})
        for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','OPENBLAS_DEFAULT_NUM_THREADS','VECLIB_MAXIMUM_THREADS','NUMEXPR_NUM_THREADS','RAYON_NUM_THREADS'):
            self.assertEqual(env[key],'1')
        self.assertEqual(env['TOKENIZERS_PARALLELISM'],'false')
        self.assertEqual(env['OMP_MAX_ACTIVE_LEVELS'],'1')
        self.assertEqual(env['INDEX_PATH'],'synthetic')


    def test_torch_api_is_applied_and_read_back_once_per_process(self):
        cpu=self.cpu();torch=Mock();torch.get_num_threads.return_value=1;torch.get_num_interop_threads.return_value=1
        with patch.dict(os.environ,{'QMD_CPU_THREADS':'1'}):
            cpu.configure_torch(torch);cpu.configure_torch(torch)
        torch.set_num_threads.assert_called_once_with(1)
        torch.set_num_interop_threads.assert_called_once_with(1)
        self.assertTrue(torch.get_num_threads.called and torch.get_num_interop_threads.called)


    def test_failed_or_ignored_torch_setting_refuses_execution(self):
        cpu=self.cpu()
        for failure in ('denied','ignored'):
            torch=Mock();torch.get_num_threads.return_value=8;torch.get_num_interop_threads.return_value=1
            if failure=='denied':torch.set_num_interop_threads.side_effect=RuntimeError('too late')
            with self.assertRaisesRegex(ValueError,'cpu_thread_configuration'):
                cpu.configure_torch(torch)


    def test_configuration_drift_is_not_silently_accepted(self):
        cpu=self.cpu();torch=Mock();torch.get_num_threads.return_value=1;torch.get_num_interop_threads.return_value=1
        cpu.configure_torch(torch);torch.get_num_threads.return_value=8
        with self.assertRaisesRegex(ValueError,'cpu_thread_configuration'):cpu.configure_torch(torch)
        torch.set_num_interop_threads.assert_called_once_with(1)


    def test_provider_cpu_refusal_precedes_dispatch_intent(self):
        cpu=self.cpu();from memvane.runtime import runtime_admission as admission
        from memvane.runtime.resource_coordination import Coordinator
        c=Coordinator(self.root);before=Mock()
        report={'ready':True,'observed_at_mono':time.monotonic(),'total_mib':16000,'available_mib':12000,'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},'memory_pressure':{'level':'normal','system_free_percent':70}}
        with patch.object(cpu.os,'cpu_count',return_value=4),patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_PROVIDER_COORD_FD':''}),patch.object(admission,'Coordinator',return_value=c),patch.object(admission,'sample',return_value=report),patch.object(admission.subprocess,'Popen',side_effect=AssertionError('must not send')):
            with cpu.lease(self.root):
                with self.assertRaises(admission.PreSendDeferred):admission.provider_popen(['synthetic'],before_launch=before)
        before.assert_not_called()

    def test_provider_child_retains_share_after_popen_returns(self):
        cpu=self.cpu();from memvane.runtime import runtime_admission as admission
        from memvane.runtime.resource_coordination import Coordinator
        c=Coordinator(self.root);held=[]
        report={'ready':True,'observed_at_mono':time.monotonic(),'total_mib':16000,'available_mib':12000,'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},'memory_pressure':{'level':'normal','system_free_percent':70}}

        def spawn(*args,**options):
            self.assertEqual(options['env']['OPENBLAS_NUM_THREADS'],'1')
            self.assertEqual(options['env']['INDEX_PATH'],'synthetic')
            held.append(os.dup(options['pass_fds'][-1]));return Mock()
        try:
            with patch.object(cpu.os,'cpu_count',return_value=4),patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_PROVIDER_COORD_FD':''}),patch.object(admission,'Coordinator',return_value=c),patch.object(admission,'sample',return_value=report),patch.object(admission.subprocess,'Popen',side_effect=spawn):
                admission.provider_popen(['synthetic'],env={'INDEX_PATH':'synthetic'})
                with self.assertRaises(ValueError):
                    with cpu.lease(self.root):self.fail('provider share lost')
        finally:
            for fd in held:os.close(fd)
