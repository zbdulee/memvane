import time
import subprocess
import sys
import json
from unittest.mock import patch
from tests.support.case import FixtureCase
from memvane.runtime import runtime_work
from memvane.runtime.resource_coordination import Coordinator, decision
from memvane.runtime.types import ResourceDeferred


class RuntimeChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        topology=patch("os.cpu_count",return_value=18);topology.start();self.addCleanup(topology.stop)
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)
        self.policy={"maxRssMib":512,"minAvailableMib":512,"maxLoadPerCpu":1}
        self.report={"observed_at_mono":time.monotonic(),"ready":True,"total_mib":16000,"available_mib":12000,"load_per_cpu":.1,
            "query_status":{"memory_pressure":"ok"},"memory_pressure":{"system_free_percent":70,"level":"normal"},"swap_used_mib":0}

    def runtime_module(self):
        return runtime_work

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
        runtime=self.runtime_module();c=Coordinator(self.root)
        with runtime.foreground('project-a',coordinator=c):
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root).step(self.policy,self.report):self.fail('background ran')
            with runtime.foreground('project-b',coordinator=Coordinator(self.root)):pass
        with c.step(self.policy,self.report):pass


    def test_durable_queue_recovers_running_job_and_coalesces_projects(self):
        runtime=self.runtime_module();q=runtime.WakeQueue(self.root)
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
        runtime=self.runtime_module();q=runtime.WakeQueue(self.root);q.put('laya-sync','/synthetic/index',now=0)
        clock=[0.];signals=iter([False,True]);calls=[]
        result=runtime.drain(q,execute=lambda row:calls.append(row['kind']) or True,
            ready=lambda:next(signals),clock=lambda:clock[0],wall=lambda:clock[0],
            sleep=lambda dt:clock.__setitem__(0,clock[0]+dt),max_runs=1,max_seconds=10)
        self.assertEqual(calls,['laya-sync']);self.assertEqual(result['runs'],1)
        self.assertGreaterEqual(clock[0],5);self.assertEqual(q.pending(),[])


    def test_wait_budget_keeps_queue_and_next_hook_can_resume(self):
        runtime=self.runtime_module();q=runtime.WakeQueue(self.root);q.put('laya-sync','/synthetic/index',now=0);clock=[0.]
        runtime.drain(q,execute=lambda _:self.fail('pressure dispatch'),ready=lambda:False,
            clock=lambda:clock[0],wall=lambda:clock[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+dt),max_seconds=6)
        self.assertEqual(len(q.pending()),1)
        runtime.drain(runtime.WakeQueue(self.root),execute=lambda _:True,ready=lambda:True,
            clock=lambda:0,wall=lambda:10,sleep=lambda _:None,max_runs=1)
        self.assertEqual(q.pending(),[])


    def test_concurrent_enqueue_cannot_be_lost_by_old_ack(self):
        runtime=self.runtime_module();q=runtime.WakeQueue(self.root);q.put('laya-sync','/synthetic/index',now=0)
        def run(row):q.put('laya-sync','/synthetic/index',now=0);return True
        runtime.drain(q,execute=run,ready=lambda:True,clock=lambda:0,wall=lambda:1,sleep=lambda _:None,max_runs=1)
        self.assertEqual(len(q.pending()),1)


    def test_index_has_priority_over_learning_and_no_second_consumer(self):
        runtime=self.runtime_module();q=runtime.WakeQueue(self.root)
        q.put('learning','/synthetic/a',now=0);q.put('laya-sync','/synthetic/index',now=0);calls=[]
        def run(row):
            nested=runtime.drain(q,execute=lambda _:self.fail('duplicate consumer'),ready=lambda:True)
            self.assertEqual(nested['status'],'busy');calls.append(row['kind']);return True
        runtime.drain(q,execute=run,ready=lambda:True,clock=lambda:0,wall=lambda:1,sleep=lambda _:None,max_runs=2)
        self.assertEqual(calls,['laya-sync','learning'])

    def test_small_child_retains_compute_lease_until_its_boundary_finishes(self):
        controller = Coordinator(self.root, clock=lambda: 100.)
        report = {**self.report, 'observed_at_mono': 100.}
        child = None
        try:
            with controller.step(self.policy, report, with_fd=True) as (_, fd):
                probe = ("import json,sys; from memvane.runtime.resource_coordination import Coordinator; "
                         "from memvane.runtime.types import ResourceDeferred\n"
                         "try:\n with Coordinator(sys.argv[1],clock=lambda:100.).step(json.loads(sys.argv[2]), "
                         "json.loads(sys.argv[3])): print('unexpected',flush=True)\n"
                         "except ResourceDeferred: print('busy',flush=True)\n"
                         "sys.stdin.read()")
                child = subprocess.Popen([sys.executable, '-B', '-c',
                    probe, str(self.root), json.dumps(self.policy), json.dumps(report)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(fd,))
                self.assertEqual(child.stdout.readline().strip(), 'busy')
            with self.assertRaises(ResourceDeferred):
                with Coordinator(self.root, clock=lambda: 100.).step(self.policy, report):
                    self.fail('inherited lease did not exclude another owner')
        finally:
            if child is not None:
                child.communicate('done', timeout=5)
        with controller.step(self.policy, report):
            pass
