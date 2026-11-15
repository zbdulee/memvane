import importlib.util
import os
from unittest.mock import Mock, patch
from tests.support.case import FixtureCase
from memvane.runtime.resource_coordination import Coordinator


class DaemonMonitor(FixtureCase):
    def setUp(self):
        super().setUp()
        topology=patch("os.cpu_count",return_value=18);topology.start();self.addCleanup(topology.stop)
        self.root=self.tmp / "state";self.root.mkdir(mode=0o700)
        self.clock=[0.]
        self.report={"ready":True,"observed_at_mono":0.,"total_mib":16000,"available_mib":12000,"load_per_cpu":.1,
          "query_status":{"memory_pressure":"ok"},"memory_pressure":{"level":"normal","system_free_percent":70}}

    def monitor(self):
        self.assertIsNotNone(importlib.util.find_spec('memvane.runtime.runtime_monitor'),'daemon monitor missing')
        from memvane.runtime import runtime_monitor
        return runtime_monitor


    def test_live_daemon_recovers_queue_after_waiter_budget_without_new_hook(self):
        module=self.monitor();from memvane.runtime import runtime_work
        q=runtime_work.WakeQueue(self.root);q.put('learning','/synthetic/a',now=0)
        c=Coordinator(self.root,clock=lambda:self.clock[0],wall=lambda:100+self.clock[0])
        reports=iter([False,True,True]);alive=iter([True,True,True,False]);wakes=[]
        def sample():return {**self.report,'ready':next(reports),'observed_at_mono':self.clock[0]}
        result=module.run(q,coordinator=c,probe=sample,wake=lambda:wakes.append(self.clock[0]),
            alive=lambda:next(alive),wait=lambda n:self.clock.__setitem__(0,self.clock[0]+n),wall=lambda:self.clock[0])
        self.assertEqual(result['status'],'parent_closed');self.assertEqual(len(wakes),1)
        self.assertEqual(len(q.pending()),1)


    def test_due_laya_wake_is_woken_without_learning_admission(self):
        module=self.monitor();from memvane.runtime import runtime_work
        q=runtime_work.WakeQueue(self.root);q.put('laya-sync','/synthetic/a',now=0)
        q.put('learning','/synthetic/b',now=0)
        c=Coordinator(self.root,clock=lambda:self.clock[0],wall=lambda:100+self.clock[0])
        alive=iter([True,True,False]);wakes=[]
        def sample():return {**self.report,'ready':False,'observed_at_mono':self.clock[0]}
        result=module.run(q,coordinator=c,probe=sample,wake=lambda:wakes.append(self.clock[0]),
            alive=lambda:next(alive),wait=lambda n:self.clock.__setitem__(0,self.clock[0]+n),wall=lambda:self.clock[0])
        self.assertEqual(result['status'],'parent_closed');self.assertEqual(len(wakes),2)
        # Learning alone still waits for admission.
        q2=runtime_work.WakeQueue(self.root);
        with q2.connect() as db:db.execute("DELETE FROM wakes WHERE kind='laya-sync'")
        alive=iter([True,False]);wakes.clear()
        module.run(q2,coordinator=c,probe=sample,wake=lambda:wakes.append(1),
            alive=lambda:next(alive),wait=lambda n:None,wall=lambda:self.clock[0])
        self.assertEqual(wakes,[])


    def test_lifecycle_pipe_stops_monitor_on_daemon_exit(self):
        module=self.monitor();read,write=os.pipe()
        try:
            self.assertTrue(module.parent_alive(read));os.close(write);write=None
            self.assertFalse(module.parent_alive(read))
        finally:
            os.close(read)
            if write is not None:os.close(write)


    def test_duplicate_monitor_is_refused_while_child_inherits_lease(self):
        module=self.monitor();held=[]
        class Child:
            pid=999991;returncode=None
            def poll(inner):return inner.returncode
            def wait(inner,**options):
                inner.returncode=0
                for fd in held:os.close(fd)
                held.clear();return 0
        def spawn(argv,**options):
            held.extend(os.dup(fd) for fd in options['pass_fds']);return Child()
        with patch.dict(os.environ, {'QMD_SETUP_GUARD_FIXTURE': ''}):
            handle=module.start(self.root,spawn=spawn)
            try:self.assertIsNone(module.start(self.root,spawn=lambda *args,**kw:self.fail('duplicate monitor')))
            finally:handle.close()


    def test_monitor_restart_is_supervised_with_backoff(self):
        module=self.monitor();clock=[0.];handle=Mock();handle.process.poll.return_value=None
        factory=Mock(return_value=handle)
        supervisor=module.Supervisor(self.root,factory=factory,clock=lambda:clock[0])
        supervisor.tick();self.assertEqual(factory.call_count,1)
        handle.process.poll.return_value=1;clock[0]=5;supervisor.tick()
        self.assertEqual(factory.call_count,1)
        clock[0]=31;supervisor.tick();self.assertEqual(factory.call_count,2)
        supervisor.close();handle.close.assert_called()


    def test_empty_queue_monitor_does_no_resource_probe_or_worker_spawn(self):
        module=self.monitor();from memvane.runtime import runtime_work
        q=runtime_work.WakeQueue(self.root);alive=iter([True,False]);waits=[]
        result=module.run(q,probe=lambda:self.fail('idle probe'),wake=lambda:self.fail('idle worker'),alive=lambda:next(alive),wait=waits.append)
        self.assertEqual(result['samples'],0);self.assertEqual(waits,[30.])
