"""CPU configuration/reservations only: no torch import, child or OS probes."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from memvane.learning.tests.support import REPO
class CpuThreads(unittest.TestCase):
    def setUp(self):
        topology=patch('os.cpu_count',return_value=18);topology.start();self.addCleanup(topology.stop)
        self.env=patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':''});self.env.start();self.addCleanup(self.env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
    def cpu(self):
        # The CPU share is runtime's; learning reaches it through runtime.api.lease.
        from memvane.runtime import api as runtime
        return runtime
    def test_standalone_adapter_finds_cpu_policy_before_request_guard(self):
        import runpy
        core=str(REPO)
        with patch.object(sys,'path',[p for p in sys.path if p!=core]):
            module=runpy.run_path(str(Path(core)/'memvane/learning/context_learning/adapter.py'),run_name='synthetic_adapter')
            self.assertIn(core,sys.path)
            with patch.dict(os.environ):
                with self.assertRaisesRegex(ValueError,'invalid_adapter_mode'):
                    module['main'](['synthetic','invalid','unused','unused'])
    def test_shared_daemon_entry_owns_a_share_without_loading_a_model(self):
        cpu=self.cpu();from memvane.learning.context_learning import inference_service as service
        def serve(root):
            with self.assertRaises(ValueError):
                with cpu.lease(root):self.fail('unreserved daemon')
        with patch('os.cpu_count',return_value=4),patch.object(service,'service_root',return_value=self.root),patch.object(service,'_serve',side_effect=serve) as call,patch.object(sys,'argv',['service','worker',str(self.root)]):
            service.main()
        call.assert_called_once_with(self.root)
    def test_adapter_configures_before_any_model_import_work(self):
        cpu=self.cpu();from memvane.learning.context_learning import adapter
        fake=Mock();fake.get_num_threads.return_value=1;fake.get_num_interop_threads.return_value=1
        with patch.dict(sys.modules,{'torch':fake}),patch.dict(os.environ,{'OMP_NUM_THREADS':'99'}):
            self.assertIs(adapter._torch(),fake)
            self.assertEqual(os.environ['OMP_NUM_THREADS'],'1')
        fake.set_num_threads.assert_called_once_with(1)
    def test_local_trainer_passes_limits_and_live_share_to_mock_child(self):
        cpu=self.cpu();from memvane.learning.context_learning import local_cycle
        from memvane.runtime.api import coordinator as Coordinator
        child=Mock(pid=999991,returncode=0);child.poll.return_value=0
        captured={}
        def spawn(argv,**options):
            captured.update(options)
            for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS'):
                self.assertEqual(options['env'][key],'1')
            self.assertEqual(options['env']['TOKENIZERS_PARALLELISM'],'false')
            with self.assertRaises(ValueError):
                with cpu.lease(self.root):self.fail('child has no CPU reservation')
            path=Path(argv[-1]);path.write_text('{}');path.chmod(0o600)
            return child
        trainer=local_cycle.LocalTrainer([sys.executable])
        with patch('os.cpu_count',return_value=4),patch('memvane.runtime.api.coordinator',return_value=Coordinator(self.root)),patch.object(local_cycle.subprocess,'Popen',side_effect=spawn),patch('memvane.kernel.process.stop_owned_checked',return_value=True):
            answer,_=trainer._call('smoke',{},self.root/'result',cwd=self.root)
        self.assertEqual(answer,{})
        self.assertTrue(captured['pass_fds'])
    def test_local_trainer_fixture_keeps_real_uid_state_closed(self):
        self.cpu();from memvane.learning.context_learning import local_cycle
        child=Mock(pid=999991,returncode=0);child.poll.return_value=0
        def spawn(argv,**options):
            self.assertEqual(options['pass_fds'],())
            path=Path(argv[-1]);path.write_text('{}');path.chmod(0o600)
            return child
        with patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'1'}),patch('memvane.runtime.api.coordinator',side_effect=AssertionError('fixture touched live root')),patch.object(local_cycle.subprocess,'Popen',side_effect=spawn),patch('memvane.kernel.process.stop_owned_checked',return_value=True):
            answer,_=local_cycle.LocalTrainer([sys.executable])._call('smoke',{},self.root/'result',cwd=self.root)
        self.assertEqual(answer,{})
    def test_excess_observed_group_cpu_uses_cooperative_stop(self):
        self.cpu();from memvane.learning.context_learning.training_watch import Watch
        clock=[0.];watch=Watch(clock=lambda:clock[0])
        report={'ready':True,'observed_at_mono':0.,'total_mib':16000,'available_mib':12000,
                'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},
                'memory_pressure':{'level':'normal','system_free_percent':70}}
        self.assertEqual(watch.observe(report,cpu_percent=150,cpu_limit=100)['action'],'yield')
        clock[0]=2.
        self.assertEqual(watch.observe({**report,'observed_at_mono':2.},cpu_percent=150,cpu_limit=100)['action'],'terminate')



if __name__ == '__main__':
    unittest.main()
