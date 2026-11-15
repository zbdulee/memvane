"""Small synthetic runtime checks: no OS pressure probes, models or user DBs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from memvane.runtime.api import coordinator as Coordinator
from memvane.learning.context_learning.local_cycle import ResourceDeferred
class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        topology=patch('os.cpu_count',return_value=18);topology.start();self.addCleanup(topology.stop)
        env=patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_SANDBOX':''})
        env.start();self.addCleanup(env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.policy={'maxRssMib':512,'minAvailableMib':512,'maxLoadPerCpu':1}
        self.report={'observed_at_mono':time.monotonic(),'ready':True,'total_mib':16000,'available_mib':12000,'load_per_cpu':.1,
            'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'system_free_percent':70,'level':'normal'},'swap_used_mib':0}
    def test_mac_resource_report_os_warning_needs_free_memory(self):
        from memvane.learning.context_learning import shared
        for free,ready in ((70,True),(8,False)):
            def probe(argv,**kw):
                data={'vm_stat':'Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 2000000.\nPages inactive: 2000000.\nPages speculative: 1000000.\n',
                    'hw.memsize':'25769803776','memory_pressure':f'System-wide memory free percentage: {free}%',
                    'kern.memorystatus_vm_pressure_level':'2','vm.swapusage':'used = 0M'}
                key=argv[-1] if argv[0].endswith('sysctl') else Path(argv[0]).name
                return subprocess.CompletedProcess(argv,0,stdout=data[key])
            with patch.object(shared.sys,'platform','darwin'),patch.object(Path,'is_file',return_value=False), \
                 patch.object(shared.subprocess,'run',side_effect=probe),patch.object(shared.os,'getloadavg',return_value=(0,0,0)):
                report=shared.resource_report(self.policy,detailed=True)
            self.assertEqual(report['ready'],ready);self.assertEqual(report['memory_pressure']['level'],'warning')
    def test_legacy_adapter_cannot_bypass_active_foreground(self):
        from memvane.learning.context_learning import local_cycle, shared
        trainer=local_cycle.LocalTrainer([sys.executable],max_rss_mib=512)
        c=Coordinator(self.root)
        with patch.dict(os.environ,{},clear=True),patch('memvane.runtime.api.coordinator',return_value=c), \
             patch.object(shared,'resource_report',return_value=self.report), \
             patch.object(local_cycle.subprocess,'Popen',side_effect=AssertionError('model must not start')):
            with c.inference(time.monotonic()+1,'foreground'):
                with self.assertRaises(ResourceDeferred):
                    trainer.call('predict',{},self.root/'result.json',cwd=self.root)
    def test_pressure_rise_stops_after_committed_optimizer_group(self):
        from memvane.learning.context_learning.training_cursor import UpdateCursor,run_updates
        clock=[self.report['observed_at_mono']]
        c=Coordinator(self.root,clock=lambda:clock[0],wall=lambda:100+clock[0]);calls=[];report=[self.report]
        request={'artifact_path':str(self.root/'candidate.artifact')};groups=[[1],[2],[3]]
        @contextmanager
        def step():
            with c.step(self.policy,report[0]) as limits:yield limits
        guard=type('Guard',(),{'step':staticmethod(step)})()
        def update(group,size):
            calls.append(group[0]);report[0]={**self.report,'memory_pressure':{'level':'warning'}}
        def save(path):path.write_bytes(b'tiny-head');path.chmod(0o600)
        with self.assertRaises(ResourceDeferred):
            run_updates(UpdateCursor(request,groups),groups,guard,update,save,lambda:{'cpu':[1],'mps':None})
        self.assertEqual(calls,[1]);self.assertEqual(UpdateCursor(request,groups).step,1)
        report[0]=self.report
        self.assertFalse(c.check(self.policy,report[0])['ready'])
        clock[0]+=5;report[0]={**self.report,'observed_at_mono':clock[0]}
        run_updates(UpdateCursor(request,groups),groups,guard,
            lambda group,size:calls.append(group[0]),save,lambda:{'cpu':[1],'mps':None})
        self.assertEqual(calls,[1,2,3])
    def test_legacy_schedule_resource_defer_does_not_become_daily_failure(self):
        from memvane.learning.context_learning import schedule
        case={'family_hashes':['task:synthetic'],'candidates':[]}
        queue={'splits':{'train':[case],'validation':[],'evaluation':[]}}
        with patch.object(schedule,'build_queue',return_value=queue),patch.object(schedule,'_load_state',return_value=None), \
             patch.object(schedule,'run_cycle',side_effect=ResourceDeferred('learning_deferred_resource',{})), \
             patch.object(schedule,'_atomic_json') as write:
            result=schedule._run_due_cycle_locked(self.root,'cycle','v',lambda:{},None,
                incumbent_artifact=self.root/'base',now=100)
        self.assertEqual(result['status'],'deferred_resource');self.assertLessEqual(result['next_due'],105)
        write.assert_not_called()



if __name__ == '__main__':
    unittest.main()
