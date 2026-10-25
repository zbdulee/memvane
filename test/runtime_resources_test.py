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

    def test_os_warning_and_unknown_pressure_defer_even_when_free_pages_look_ok(self):
        for pressure in ({'system_free_percent':70,'level':'warning'},None):
            self.assertFalse(decision(self.policy,{**self.report,'memory_pressure':pressure})['ready'])

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

    def test_background_operation_releases_compute_before_pressure_wait(self):
        runtime=self.runtime();calls=[];clock=[self.report['observed_at_mono']]
        c=Coordinator(self.root,clock=lambda:clock[0],wall=lambda:100+clock[0])
        def execute(argv,**kw):calls.append(argv);return subprocess.CompletedProcess(argv,0)
        with patch.object(runtime,'sample',return_value={**self.report,'ready':False}):
            self.assertEqual(runtime.qmd_operation(['/synthetic/qmd','embed'],coordinator=c,execute=execute),75)
        self.assertEqual(calls,[])
        with c.inference_compute(time.monotonic()+.1):pass
        with patch.object(runtime,'sample',return_value=self.report):
            self.assertEqual(runtime.qmd_operation(['/synthetic/qmd','embed'],coordinator=c,execute=execute),75)
            clock[0]+=5;self.report['observed_at_mono']=clock[0]
            self.assertEqual(runtime.qmd_operation(['/synthetic/qmd','embed'],coordinator=c,execute=execute),0)
        self.assertEqual(len(calls),1)

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

    def test_recall_advertises_priority_through_context_injection(self):
        import recall
        runtime=self.runtime();inside=[]
        @contextmanager
        def priority(*a,**kw):
            inside.append(True)
            try:yield
            finally:inside.pop()
        self.assertTrue(hasattr(recall,'_main'),'recall priority wrapper missing')
        with patch.dict(os.environ,{},clear=True),patch.object(runtime,'foreground',priority), \
             patch.object(recall,'_main',side_effect=lambda:self.assertEqual(inside,[True])):
            recall.main()
        self.assertEqual(inside,[])

    def test_hook_completion_wakes_persistent_work_without_changing_exit(self):
        import hook_main
        runtime=self.runtime()
        with patch.object(runtime,'hook_wake') as wake:
            self.assertEqual(hook_main.run(lambda:None),0)
            wake.assert_called_once()

    def test_mac_resource_report_rejects_os_warning(self):
        from context_learning import shared
        def probe(argv,**kw):
            data={'vm_stat':'Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 2000000.\nPages inactive: 2000000.\nPages speculative: 1000000.\n',
                'hw.memsize':'25769803776','memory_pressure':'System-wide memory free percentage: 70%',
                'kern.memorystatus_vm_pressure_level':'2','vm.swapusage':'used = 0M'}
            key=argv[-1] if argv[0].endswith('sysctl') else Path(argv[0]).name
            return subprocess.CompletedProcess(argv,0,stdout=data[key])
        with patch.object(shared.sys,'platform','darwin'),patch.object(Path,'is_file',return_value=False), \
             patch.object(shared.subprocess,'run',side_effect=probe),patch.object(shared.os,'getloadavg',return_value=(0,0,0)):
            report=shared.resource_report(self.policy,detailed=True)
        self.assertFalse(report['ready']);self.assertEqual(report['memory_pressure']['level'],'warning')

    def test_dirty_enqueue_persists_recovery_hint_after_content(self):
        import dirty_queue
        runtime=self.runtime();path=self.root/'dirty-queue'
        def request(kind,**kw):
            self.assertEqual(kind,'index');self.assertTrue(path.read_text().startswith('synthetic\t'))
            self.assertFalse(kw['start'])
        with patch.object(dirty_queue,'queue_path',return_value=path),patch.object(runtime,'request',side_effect=request) as hint:
            dirty_queue.enqueue_collections({'synthetic':'/synthetic/docs'})
        hint.assert_called_once()

    def test_update_worker_keeps_job_until_embed_actually_completes(self):
        import hashlib
        import update_hook_queue as update
        import config,setup_guard,qmd_route
        from context_learning import synthetic_scope
        runtime=self.runtime();cwd=str(self.root);key=hashlib.sha256(cwd.encode()).hexdigest()[:32]
        job=self.root/(key+'.job.json');phase=['embed_failed']
        update._write(job,{'schema':'qmd-update-hook-job-v1','cwd':cwd})
        def run(argv,**kw):
            update._write(self.root/(key+'.status.json'),{'runId':kw['env']['QMD_UPDATE_HOOK_RUN_ID'],'status':phase[0]})
            return subprocess.CompletedProcess(argv,0,stdout='',stderr='')
        with patch.dict(os.environ,{},clear=True),patch.object(update,'_private_queue',return_value=self.root), \
             patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(synthetic_scope,'invocation_in_generated_story',return_value=True), \
             patch.object(setup_guard,'status',return_value={'status':'ready'}), \
             patch.object(qmd_route,'project_paths',return_value={'selected':True}), \
             patch.object(update,'readiness'),patch.object(update.subprocess,'run',side_effect=run), \
             patch.object(runtime,'request') as hint:
            update.worker(key);self.assertTrue(job.exists());hint.assert_called_with('update',key)
            phase[0]='completed';update.worker(key);self.assertFalse(job.exists())

    def test_small_qmd_child_obeys_the_shared_boundary(self):
        runtime=self.runtime()
        with patch.dict(os.environ,{},clear=True),patch.object(runtime,'sample',return_value=self.report):
            code=runtime.qmd_operation([sys.executable,'-B','-c','pass'],coordinator=Coordinator(self.root))
        self.assertEqual(code,0)

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

    def test_learning_wake_defers_during_install_transition(self):
        import config,setup_guard
        from context_learning import shared
        runtime=self.runtime()
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.root), \
             patch.object(setup_guard,'status',return_value={'status':'setup_required'}), \
             patch.object(shared,'run_training_pending',return_value={'status':'staged'}) as train:
            self.assertFalse(runtime.dispatch({'kind':'learning','target':str(self.root)}))
        train.assert_not_called()

    def test_cleanup_deferral_keeps_pending_without_consuming_cooldown(self):
        import orphan_reclaim as reclaim
        runtime=self.runtime();pending=self.root/'cleanup-pending';pending.write_text('pending')
        with patch.object(reclaim,'decide',return_value={'action':'reclaim','trigger':'post-remove'}), \
             patch.object(reclaim,'lock_dir',return_value=self.root/'cleanup-lock'), \
             patch.object(reclaim,'pending_path',return_value=pending), \
             patch.object(reclaim,'failed_path',return_value=self.root/'cleanup-failed'), \
             patch.object(reclaim,'touch_cooldown') as cooldown,patch.object(reclaim,'log'), \
             patch.object(runtime,'Coordinator',return_value=Coordinator(self.root)), \
             patch.object(runtime,'default_root',return_value=self.root),patch.object(runtime,'wake'), \
             patch.object(runtime,'sample',return_value={**self.report,'ready':False}), \
             patch.object(reclaim.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='',stderr='')) as launch:
            result=reclaim.run(str(self.root),qmd_bin='/synthetic/qmd')
        self.assertEqual(result.get('reason'),'resource_deferred')
        self.assertTrue(pending.exists());cooldown.assert_not_called();launch.assert_not_called()
        self.assertEqual(runtime.WakeQueue(self.root).pending()[0]['kind'],'cleanup')

    def test_learning_dispatch_keeps_existing_history_worker_fence(self):
        import config,setup_guard
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
             patch.object(setup_guard,'status',return_value={'status':'ready'}), \
             patch.object(history_lifecycle,'worker_lease',lease), \
             patch.object(shared,'learning_worker_step',side_effect=step):
            self.assertTrue(runtime.dispatch({'kind':'learning','target':str(self.root),'revision':1}))

    def test_cleanup_retry_resolves_its_own_index_route(self):
        import setup_guard,qmd_route
        runtime=self.runtime();route={key:'/synthetic/'+key for key in qmd_route.PATH_KEYS}
        def execute(argv,**kw):
            self.assertEqual({key:kw['env'][key] for key in route},route)
            self.assertEqual(argv[-3:],['--cwd',str(self.root),'--json'])
            return subprocess.CompletedProcess(argv,0,stdout='{"reason":"resource_deferred"}')
        with patch.dict(os.environ,{'INDEX_PATH':'/unrelated/project'}), \
             patch.object(setup_guard,'status',return_value={'status':'ready'}), \
             patch.object(qmd_route,'project_paths',return_value=route), \
             patch.object(runtime.subprocess,'run',side_effect=execute):
            self.assertFalse(runtime.dispatch({'kind':'cleanup','target':str(self.root)}))

    def test_cleanup_attempt_records_cooldown_before_the_command_can_crash(self):
        import orphan_reclaim as reclaim
        runtime=self.runtime();started=[]
        def execute(argv,**kw):
            self.assertEqual(started,['attempt'])
            return subprocess.CompletedProcess(argv,1,stdout='',stderr='synthetic failure')
        with patch.object(reclaim,'decide',return_value={'action':'reclaim'}), \
             patch.object(reclaim,'lock_dir',return_value=self.root/'cleanup-lock'),patch.object(reclaim,'log'), \
             patch.object(reclaim,'failed_path',return_value=self.root/'cleanup-failed'), \
             patch.object(reclaim,'touch_cooldown',side_effect=lambda:started.append('attempt')), \
             patch.object(runtime,'Coordinator',return_value=Coordinator(self.root)), \
             patch.object(runtime,'sample',return_value=self.report), \
             patch.object(reclaim.subprocess,'run',side_effect=execute):
            result=reclaim.run(str(self.root),qmd_bin='/synthetic/qmd')
        self.assertFalse(result['ok']);self.assertEqual(started,['attempt'])


if __name__=='__main__':unittest.main()
