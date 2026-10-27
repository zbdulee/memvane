"""Small synthetic runtime checks: no OS pressure probes, models or user DBs."""
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning.resource_coordination import Coordinator,decision
from context_learning.local_cycle import ResourceDeferred


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

    def runtime(self):
        self.assertIsNotNone(importlib.util.find_spec('runtime_work'),'runtime wake queue missing')
        return importlib.import_module('runtime_work')

    def test_learning_wake_defers_during_install_transition(self):
        import config
        from context_learning import shared
        runtime=self.runtime()
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.root), \
             patch('context_learning.laya_learning.ready',return_value=False), \
             patch.object(shared,'run_training_pending',return_value={'status':'staged'}) as train:
            self.assertFalse(runtime.dispatch({'kind':'learning','target':str(self.root)}))
        train.assert_not_called()


    def test_unknown_critical_or_scarce_warning_pressure_defer(self):
        for pressure in (None,{'system_free_percent':70,'level':'critical'},
                         {'system_free_percent':5,'level':'warning'}):
            self.assertFalse(decision(self.policy,{**self.report,'memory_pressure':pressure})['ready'])
        # Swap alone makes macOS say 'warning'; ample free memory still admits.
        self.assertTrue(decision(self.policy,{**self.report,'memory_pressure':
                                 {'system_free_percent':70,'level':'warning'}})['ready'])

    def test_low_relative_headroom_uses_one_batch_and_absolute_budget_defers(self):
        self.assertEqual(decision(self.policy,{**self.report,'available_mib':6500})['microbatch_size'],1)
        self.assertTrue(decision(self.policy,{**self.report,'available_mib':5000})['ready'])
        self.assertFalse(decision(self.policy,{**self.report,'available_mib':1023})['ready'])

    def test_cross_session_foreground_blocks_background_but_not_search(self):
        runtime=self.runtime();c=Coordinator(self.root)
        with runtime.foreground('project-a',coordinator=c):
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).step(self.policy,self.report):self.fail('background ran')
            with runtime.foreground('project-b',coordinator=Coordinator(self.root)):pass
        with c.step(self.policy,self.report):pass

    def test_durable_queue_recovers_running_job_and_coalesces_projects(self):
        runtime=self.runtime();q=runtime.WakeQueue(self.root)
        q.put('learning','/synthetic/a',now=0);q.put('learning','/synthetic/a',now=0)
        q.put('learning','/synthetic/b',now=0)
        rows=q.pending();self.assertEqual(len(rows),2)
        with q.connect() as db:db.execute("UPDATE wakes SET status='running'")
        restarted=runtime.WakeQueue(self.root);calls=[]
        result=runtime.drain(restarted,execute=lambda row:calls.append(row['target']) or True,
            ready=lambda:True,clock=lambda:0,wall=lambda:1,sleep=lambda _:None,max_runs=2)
        self.assertEqual(result['runs'],2);self.assertEqual(set(calls),{'/synthetic/a','/synthetic/b'})
        self.assertEqual(restarted.pending(),[])

    def test_fake_pressure_recovers_without_another_hook(self):
        runtime=self.runtime();q=runtime.WakeQueue(self.root);q.put('index','',now=0)
        clock=[0.];signals=iter([False,True]);calls=[]
        result=runtime.drain(q,execute=lambda row:calls.append(row['kind']) or True,
            ready=lambda:next(signals),clock=lambda:clock[0],wall=lambda:clock[0],
            sleep=lambda dt:clock.__setitem__(0,clock[0]+dt),max_runs=1,max_seconds=10)
        self.assertEqual(calls,['index']);self.assertEqual(result['runs'],1)
        self.assertGreaterEqual(clock[0],5);self.assertEqual(q.pending(),[])

    def test_wait_budget_keeps_queue_and_next_hook_can_resume(self):
        runtime=self.runtime();q=runtime.WakeQueue(self.root);q.put('index','',now=0);clock=[0.]
        runtime.drain(q,execute=lambda _:self.fail('pressure dispatch'),ready=lambda:False,
            clock=lambda:clock[0],wall=lambda:clock[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+dt),max_seconds=6)
        self.assertEqual(len(q.pending()),1)
        runtime.drain(runtime.WakeQueue(self.root),execute=lambda _:True,ready=lambda:True,
            clock=lambda:0,wall=lambda:10,sleep=lambda _:None,max_runs=1)
        self.assertEqual(q.pending(),[])

    def test_concurrent_enqueue_cannot_be_lost_by_old_ack(self):
        runtime=self.runtime();q=runtime.WakeQueue(self.root);q.put('index','',now=0)
        def run(row):q.put('index','',now=0);return True
        runtime.drain(q,execute=run,ready=lambda:True,clock=lambda:0,wall=lambda:1,sleep=lambda _:None,max_runs=1)
        self.assertEqual(len(q.pending()),1)

    def test_index_has_priority_over_learning_and_no_second_consumer(self):
        runtime=self.runtime();q=runtime.WakeQueue(self.root)
        q.put('learning','/synthetic/a',now=0);q.put('index','',now=0);calls=[]
        def run(row):
            nested=runtime.drain(q,execute=lambda _:self.fail('duplicate consumer'),ready=lambda:True)
            self.assertEqual(nested['status'],'busy');calls.append(row['kind']);return True
        runtime.drain(q,execute=run,ready=lambda:True,clock=lambda:0,wall=lambda:1,sleep=lambda _:None,max_runs=2)
        self.assertEqual(calls,['index','learning'])

    def test_small_other_process_cannot_duplicate_compute(self):
        c=Coordinator(self.root)
        code="from pathlib import Path; from context_learning.resource_coordination import Coordinator; from context_learning.local_cycle import ResourceDeferred; import json,sys\ntry:\n with Coordinator(Path(sys.argv[1])).step(json.loads(sys.argv[2]),json.loads(sys.argv[3])):sys.exit(9)\nexcept ResourceDeferred:sys.exit(0)"
        with c.step(self.policy,self.report):
            p=subprocess.run([sys.executable,'-B','-c',code,str(self.root),json.dumps(self.policy),json.dumps(self.report)],
                env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'core')},timeout=5,check=False)
        self.assertEqual(p.returncode,0)

    def test_hook_completion_wakes_persistent_work_without_changing_exit(self):
        import hook_main
        runtime=self.runtime()
        with patch.object(runtime,'hook_wake') as wake:
            self.assertEqual(hook_main.run(lambda:None),0)
            wake.assert_called_once()

    def test_mac_resource_report_os_warning_needs_free_memory(self):
        from context_learning import shared
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
        from context_learning import local_cycle,shared,resource_coordination
        trainer=local_cycle.LocalTrainer([sys.executable],max_rss_mib=512)
        c=Coordinator(self.root)
        with patch.dict(os.environ,{},clear=True),patch.object(resource_coordination,'Coordinator',return_value=c), \
             patch.object(shared,'resource_report',return_value=self.report), \
             patch.object(local_cycle.subprocess,'Popen',side_effect=AssertionError('model must not start')):
            with c.inference(time.monotonic()+1,'foreground'):
                with self.assertRaises(ResourceDeferred):
                    trainer.call('predict',{},self.root/'result.json',cwd=self.root)

    def test_pressure_rise_stops_after_committed_optimizer_group(self):
        from context_learning.training_cursor import UpdateCursor,run_updates
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
        from context_learning import schedule
        case={'family_hashes':['task:synthetic'],'candidates':[]}
        queue={'splits':{'train':[case],'validation':[],'evaluation':[]}}
        with patch.object(schedule,'build_queue',return_value=queue),patch.object(schedule,'_load_state',return_value=None), \
             patch.object(schedule,'run_cycle',side_effect=ResourceDeferred('learning_deferred_resource',{})), \
             patch.object(schedule,'_atomic_json') as write:
            result=schedule._run_due_cycle_locked(self.root,'cycle','v',lambda:{},None,
                incumbent_artifact=self.root/'base',now=100)
        self.assertEqual(result['status'],'deferred_resource');self.assertLessEqual(result['next_due'],105)
        write.assert_not_called()

    def test_small_child_retains_compute_lease_until_its_boundary_finishes(self):
        c=Coordinator(self.root);child=None
        try:
            with c.step(self.policy,self.report,with_fd=True) as (_,fd):
                child=subprocess.Popen([sys.executable,'-B','-c',"import sys; print('ready',flush=True); sys.stdin.read()"],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,pass_fds=(fd,))
                self.assertEqual(child.stdout.readline().strip(),'ready')
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).step(self.policy,self.report):self.fail('orphan overlap')
        finally:
            if child is not None:child.communicate('done',timeout=2)
        with c.step(self.policy,self.report):pass

    def test_learning_dispatch_keeps_existing_history_worker_fence(self):
        import config
        from context_learning import shared,history_lifecycle
        runtime=self.runtime();inside=[]
        @contextmanager
        def lease():
            inside.append(True)
            try:yield
            finally:inside.pop()
        def step(*a):
            self.assertEqual(inside,[True]);return {'status':'settled'}
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.root), \
             patch('context_learning.laya_learning.ready',return_value=True), \
             patch.object(history_lifecycle,'worker_lease',lease), \
             patch.object(shared,'learning_worker_step',side_effect=step):
            self.assertTrue(runtime.dispatch({'kind':'learning','target':str(self.root),'revision':1}))

if __name__=='__main__':unittest.main()
