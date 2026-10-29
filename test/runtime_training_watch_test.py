"""Running-training control and daemon recovery; no ML, OS probes or host CLI."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning.local_cycle import ResourceDeferred
from context_learning.resource_coordination import Coordinator


class RunningTraining(unittest.TestCase):
    def setUp(self):
        topology=patch('os.cpu_count',return_value=18);topology.start();self.addCleanup(topology.stop)
        self.env=patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_SANDBOX':''});self.env.start();self.addCleanup(self.env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.clock=[0.]
        self.report={'ready':True,'observed_at_mono':0.,'total_mib':16000,
            'available_mib':12000,'load_per_cpu':.1,'query_status':{'memory_pressure':'ok'},
            'memory_pressure':{'level':'normal','system_free_percent':70}}

    def watch(self):
        self.assertIsNotNone(importlib.util.find_spec('context_learning.training_watch'),'running watchdog missing')
        from context_learning import training_watch
        return training_watch

    @staticmethod
    def group_gone_after_signal(pid,sig):
        if sig==0:raise ProcessLookupError()

    def test_running_pressure_requests_yield_then_forces_stop_after_grace(self):
        w=self.watch().Watch(clock=lambda:self.clock[0])
        self.assertEqual(w.observe(self.report)['action'],'continue')
        self.clock[0]=.5
        result=w.observe({**self.report,'load_per_cpu':1.5,'observed_at_mono':.5})
        self.assertEqual(result['action'],'yield')
        self.clock[0]=2.5
        self.assertEqual(w.observe({**self.report,'observed_at_mono':2.5})['action'],'terminate')

    def test_foreground_request_uses_same_bounded_yield_path(self):
        w=self.watch().Watch(clock=lambda:self.clock[0])
        self.assertEqual(w.observe(self.report,foreground=True)['action'],'yield')
        self.clock[0]=2.1
        self.assertEqual(w.observe({**self.report,'observed_at_mono':2.1},foreground=True)['action'],'terminate')

    def test_index_update_request_uses_cooperative_yield_path(self):
        w=self.watch().Watch(clock=lambda:self.clock[0])
        self.assertEqual(w.observe(self.report,priority_work=True),
                         {'action':'yield','reason':'priority_index_work'})
        self.clock[0]=2.1
        self.assertEqual(w.observe({**self.report,'observed_at_mono':2.1},priority_work=True)['action'],'yield')

    def test_pressure_after_priority_request_retains_bounded_stop(self):
        w=self.watch().Watch(clock=lambda:self.clock[0])
        self.assertEqual(w.observe(self.report,priority_work=True)['action'],'yield')
        self.clock[0]=.5
        self.assertEqual(w.observe({**self.report,'load_per_cpu':1.5,'observed_at_mono':.5},
                                   priority_work=True)['action'],'yield')
        self.clock[0]=2.5
        self.assertEqual(w.observe({**self.report,'observed_at_mono':2.5},
                                   priority_work=True)['action'],'terminate')

    def test_late_pressure_gets_its_own_grace_after_priority_yield(self):
        w=self.watch().Watch(clock=lambda:self.clock[0])
        self.assertEqual(w.observe(self.report,priority_work=True)['action'],'yield')
        self.clock[0]=3.
        stressed={**self.report,'load_per_cpu':1.5,'observed_at_mono':3.}
        self.assertEqual(w.observe(stressed,priority_work=True)['action'],'yield')
        self.clock[0]=5.1
        self.assertEqual(w.observe({**self.report,'observed_at_mono':5.1},
                                   priority_work=True)['action'],'terminate')

    def test_due_index_wake_is_visible_without_mutating_the_queue(self):
        import runtime_work
        queue=runtime_work.WakeQueue(self.root)
        controller=Coordinator(self.root)
        self.assertFalse(controller.priority_work())
        queue.put('learning',str(self.root/'project'),now=0)
        self.assertFalse(controller.priority_work())
        key=queue.put('index','',now=time.time()+60)
        self.assertFalse(controller.priority_work())
        with queue.connect() as db:db.execute('UPDATE wakes SET due=? WHERE id=?',(time.time()-1,key))
        self.assertTrue(controller.priority_work())
        policy={'maxRssMib':512,'minAvailableMib':768,'maxLoadPerCpu':1}
        with self.assertRaisesRegex(ResourceDeferred,'learning_deferred_priority_work'):
            with Coordinator(self.root,clock=lambda:0).training(policy,self.report):
                self.fail('training admitted before due index')
        self.assertEqual(len(queue.pending()),2)
        with queue.connect() as db:db.execute('DELETE FROM wakes WHERE id=?',(key,))
        self.assertFalse(controller.priority_work())
        key=queue.put('laya-sync',str(self.root/'project'),now=time.time()-1)
        self.assertTrue(controller.priority_work())
        with queue.connect() as db:db.execute('DELETE FROM wakes WHERE id=?',(key,))
        self.assertFalse(controller.priority_work())

    def test_unsafe_wake_database_defers_training(self):
        path=self.root/'runtime-wakes.sqlite3';path.symlink_to(self.root/'missing')
        self.assertTrue(Coordinator(self.root).priority_work())

    def test_critical_and_unknown_pressure_do_not_wait_for_checkpoint(self):
        for report in (None,{**self.report,'observed_at_mono':-10},
                {**self.report,'memory_pressure':{'level':'critical','system_free_percent':2}},
                {**self.report,'available_mib':100}):
            w=self.watch().Watch(clock=lambda:self.clock[0])
            self.assertEqual(w.observe(report)['action'],'kill')

    def test_stop_pipe_is_observed_without_an_os_probe(self):
        module=self.watch();read,write=os.pipe()
        try:
            module.poll_stop(read)
            os.write(write,b'y')
            with self.assertRaisesRegex(ValueError,'learning_yield'):module.poll_stop(read)
        finally:os.close(read);os.close(write)

    def test_parent_loss_also_stops_at_next_cooperative_boundary(self):
        module=self.watch();read,write=os.pipe();os.close(write)
        try:
            with self.assertRaisesRegex(ValueError,'learning_yield'):module.poll_stop(read)
        finally:os.close(read)

    def test_emergency_cleanup_signals_only_owned_group_and_never_waits_unbounded(self):
        module=self.watch();process=Mock(pid=999991)
        process.poll.return_value=None;process.wait.return_value=-9
        with patch.object(module.os,'killpg',side_effect=self.group_gone_after_signal) as send:
            self.assertTrue(module.stop_owned(process,emergency=True))
        self.assertEqual(send.call_args_list,[unittest.mock.call(999991,signal.SIGKILL),unittest.mock.call(999991,0)])
        self.assertTrue(process.wait.call_args.kwargs['timeout']<=1)

    def test_cleanup_denial_is_not_success(self):
        module=self.watch();process=Mock(pid=999991);process.poll.return_value=None
        with patch.object(module.os,'killpg',side_effect=PermissionError('denied')):
            self.assertFalse(module.stop_owned(process,emergency=True))

    def test_exited_leader_does_not_hide_surviving_descendants(self):
        module=self.watch();process=Mock(pid=999991)
        process.poll.return_value=0;process.wait.return_value=0
        with patch.object(module.os,'killpg',side_effect=PermissionError('descendant denied')) as send:
            self.assertFalse(module.stop_owned(process))
        send.assert_called()

    def test_lingering_group_after_kill_is_unknown_with_bounded_wait(self):
        module=self.watch();process=Mock(pid=999991)
        process.wait.return_value=0
        with patch.object(module.os,'killpg'),patch.object(module.time,'monotonic',side_effect=lambda:self.clock[0]),patch.object(module.time,'sleep',side_effect=lambda n:self.clock.__setitem__(0,self.clock[0]+n)):
            self.assertFalse(module.stop_owned(process,emergency=True))
        self.assertLessEqual(self.clock[0],.51)

    def test_cleanup_unknown_blocks_new_common_compute_after_claim_release(self):
        c=Coordinator(self.root)
        c.cleanup_unknown(999991)
        with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
            with c._gate('compute.lock'):self.fail('unknown group admitted')
        self.assertTrue((self.root/'cleanup-reservation.json').is_file())
        original=(self.root/'cleanup-reservation.json').read_bytes();c.cleanup_unknown(999992)
        self.assertEqual((self.root/'cleanup-reservation.json').read_bytes(),original)
        for lane in ('provider','startup'):
            with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
                with c.activity(lane,{},self.report):self.fail('unknown group admitted')
        with self.assertRaisesRegex(ResourceDeferred,'cleanup_recovery_required'):
            with c.training({},self.report):self.fail('unknown group admitted')

    def test_successful_leader_cannot_ack_when_descendant_cleanup_is_denied(self):
        self.watch();from context_learning import local_cycle
        process=Mock(pid=999991,returncode=0);process.poll.return_value=0
        def spawn(argv,**options):
            output=Path(argv[-1]);output.write_text('{}');output.chmod(0o600)
            return process
        trainer=local_cycle.LocalTrainer([sys.executable])
        with patch('context_learning.resource_coordination.Coordinator',return_value=Coordinator(self.root)),patch.object(local_cycle.subprocess,'Popen',side_effect=spawn),patch.object(local_cycle.os,'killpg',side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ResourceDeferred,'trainer_cleanup_unavailable'):
                trainer._call('smoke',{},self.root/'result',cwd=self.root)
        self.assertFalse((self.root/'result').exists())
        self.assertTrue((self.root/'cleanup-reservation.json').is_file())
        self.assertTrue((self.root/'result.pending').is_file())

    def test_controlled_running_child_is_observed_and_emergency_stopped(self):
        self.watch();from context_learning import local_cycle
        process=Mock(pid=999991,returncode=None)
        process.poll.side_effect=lambda:process.returncode
        def wait(**options):process.returncode=-9;return -9
        process.wait.side_effect=wait
        healthy={**self.report,'observed_at_mono':0.}
        critical={**healthy,'memory_pressure':{'level':'critical','system_free_percent':2}}
        reports=iter([healthy,critical,critical])
        trainer=local_cycle.LocalTrainer([sys.executable],max_seconds=1,pressure_probe=lambda **options:next(reports),require_monitor=True)
        trainer.learning_control={'policy':{'maxLoadPerCpu':1}}
        stats=subprocess.CompletedProcess([],0,stdout='999991 1.0 1024\n')
        with patch('context_learning.resource_coordination.Coordinator',return_value=Coordinator(self.root)),patch.object(local_cycle.subprocess,'Popen',return_value=process),patch.object(local_cycle.subprocess,'run',return_value=stats),patch.object(local_cycle.time,'monotonic',side_effect=lambda:self.clock[0]),patch.object(local_cycle.time,'sleep',side_effect=lambda n:self.clock.__setitem__(0,self.clock[0]+n)),patch.object(local_cycle.os,'killpg',side_effect=self.group_gone_after_signal) as send:
            with self.assertRaises(ResourceDeferred):
                trainer._call('train',{'artifact_path':str(self.root/'head')},self.root/'result',cwd=self.root)
        self.assertIn(unittest.mock.call(999991,signal.SIGKILL),send.call_args_list)
        self.assertLessEqual(self.clock[0],1.)
        self.assertFalse(json.loads((self.root/'resource-defer.json').read_text())['completionAck'])

    def test_foreground_stop_reaches_owned_parent_and_has_bounded_grace(self):
        self.watch();from context_learning import local_cycle
        held=[];process=Mock(pid=999991,returncode=None)
        process.poll.side_effect=lambda:process.returncode
        def spawn(*args,**options):held.append(os.dup(options['pass_fds'][-1]));return process
        def wait(**options):process.returncode=-15;return -15
        process.wait.side_effect=wait
        report=lambda **options:{**self.report,'observed_at_mono':self.clock[0]}
        trainer=local_cycle.LocalTrainer([sys.executable],max_seconds=10,pressure_probe=report,require_monitor=True)
        trainer.learning_control={'policy':{'maxLoadPerCpu':1}}
        coordinator=Coordinator(self.root);coordinator.demand=Mock(return_value=True)
        stats=subprocess.CompletedProcess([],0,stdout='999991 1.0 1024\n')
        try:
            with patch('context_learning.resource_coordination.Coordinator',return_value=coordinator),patch.object(local_cycle.subprocess,'Popen',side_effect=spawn),patch.object(local_cycle.subprocess,'run',return_value=stats),patch.object(local_cycle.time,'monotonic',side_effect=lambda:self.clock[0]),patch.object(local_cycle.time,'sleep',side_effect=lambda n:self.clock.__setitem__(0,self.clock[0]+n)),patch.object(local_cycle.os,'killpg',side_effect=self.group_gone_after_signal) as send:
                with self.assertRaises(ResourceDeferred):trainer._call('train',{},self.root/'result',cwd=self.root)
            self.assertEqual(os.read(held[0],1),b'y')
            self.assertEqual(send.call_args_list,[unittest.mock.call(999991,signal.SIGTERM),unittest.mock.call(999991,signal.SIGKILL),unittest.mock.call(999991,0)])
            self.assertTrue(2<=self.clock[0]<=2.5)
        finally:
            for fd in held:os.close(fd)

    def test_real_training_guard_reads_parent_stop_pipe(self):
        self.watch();from context_learning import resource_coordination as coordination
        read,write=os.pipe()
        try:
            c=Coordinator(self.root)
            with c._gate('trainer.lock') as fd,patch.object(coordination,'default_root',return_value=self.root),patch.dict(os.environ,{'QMD_TRAINER_COORD_FD':str(fd),'QMD_TRAINER_STOP_FD':str(read)}):
                guard=coordination.TrainingGuard({'schema':1,'root':str(self.root),'policy':{},'namespace':'synthetic','job_id':'job'})
                guard.poll();os.write(write,b'y')
                with self.assertRaisesRegex(ValueError,'learning_yield'):guard.poll()
        finally:os.close(read);os.close(write)

    def test_training_guard_yields_before_next_group_for_due_update(self):
        import runtime_work
        from context_learning import resource_coordination as coordination
        controller=Coordinator(self.root)
        with controller._gate('trainer.lock') as fd,patch.object(coordination,'default_root',return_value=self.root),patch.dict(os.environ,{'QMD_TRAINER_COORD_FD':str(fd),'QMD_TRAINER_STOP_FD':''}):
            guard=coordination.TrainingGuard({'schema':1,'root':str(self.root),'policy':{},'namespace':'synthetic','job_id':'job'})
            guard.poll()
            runtime_work.WakeQueue(self.root).put('update','a'*32,now=time.time()-1)
            with self.assertRaisesRegex(ValueError,'learning_yield'):guard.poll()

    def test_final_head_can_recover_from_completed_cursor_without_another_train(self):
        self.watch()
        from context_learning import training_cursor as cursor
        request={'artifact_path':str(self.root/'candidate.artifact')};groups=[[1]]
        state=cursor.UpdateCursor(request,groups)
        def save(path):path.write_bytes(b'normal-head');path.chmod(0o600)
        state.commit(1,save,{'cpu':[1,2],'mps':None})
        self.assertTrue(cursor.recover_completed(request))
        self.assertEqual(Path(request['artifact_path']).read_bytes(),b'normal-head')
        self.assertIsNotNone(cursor.completion(request))

    def test_unknown_final_artifact_is_preserved_instead_of_replaced(self):
        self.watch();from context_learning import training_cursor as cursor
        request={'artifact_path':str(self.root/'candidate.artifact')};state=cursor.UpdateCursor(request,[[1]])
        def save(path):path.write_bytes(b'normal-head');path.chmod(0o600)
        state.commit(1,save,{'cpu':[1],'mps':None})
        path=Path(request['artifact_path']);path.write_bytes(b'uncertain');path.chmod(0o600)
        with self.assertRaises(ValueError):cursor.recover_completed(request)
        self.assertEqual(path.read_bytes(),b'uncertain')

    def test_changed_cursor_count_cannot_finalize_an_incomplete_plan(self):
        self.watch();from context_learning import training_cursor as cursor
        request={'artifact_path':str(self.root/'candidate.artifact')}
        state=cursor.UpdateCursor(request,[[1],[2]])
        def save(path):path.write_bytes(b'first-group');path.chmod(0o600)
        state.commit(1,save,{'cpu':[1],'mps':None})
        row=json.loads(state.path.read_text());row['total_groups']=1
        state.path.write_text(json.dumps(row))
        self.assertFalse(cursor.recover_completed(request))
        self.assertFalse(Path(request['artifact_path']).exists())

    def test_cursor_without_independent_plan_requires_actual_group_validation(self):
        self.watch();from context_learning import training_cursor as cursor
        request={'artifact_path':str(self.root/'candidate.artifact')}
        state=cursor.UpdateCursor(request,[[1]])
        def save(path):path.write_bytes(b'head');path.chmod(0o600)
        state.commit(1,save,{'cpu':[1],'mps':None})
        (state.root/'group-plan.json').unlink()  # Synthetic legacy receipt.
        self.assertFalse(cursor.recover_completed(request))
        cursor.UpdateCursor(request,[[1]])
        self.assertTrue(cursor.recover_completed(request))

    def test_inactive_slot_interruption_keeps_last_normal_cursor(self):
        self.watch();from context_learning import training_cursor as cursor
        request={'artifact_path':str(self.root/'candidate.artifact')};groups=[[1],[2]]
        state=cursor.UpdateCursor(request,groups)
        def save(path):path.write_bytes(b'normal-head');path.chmod(0o600)
        state.commit(1,save,{'cpu':[1],'mps':None})
        def interrupted(path):path.write_bytes(b'partial');path.chmod(0o600);raise RuntimeError('mock power loss')
        with self.assertRaises(RuntimeError):state.commit(2,interrupted,{'cpu':[2],'mps':None})
        resumed=cursor.UpdateCursor(request,groups)
        self.assertEqual(resumed.step,1);self.assertEqual(resumed.head().read_bytes(),b'normal-head')


class DaemonMonitor(unittest.TestCase):
    setUp=RunningTraining.setUp
    def monitor(self):
        self.assertIsNotNone(importlib.util.find_spec('runtime_monitor'),'daemon monitor missing')
        import runtime_monitor
        return runtime_monitor

    def test_live_daemon_recovers_queue_after_waiter_budget_without_new_hook(self):
        module=self.monitor();import runtime_work
        q=runtime_work.WakeQueue(self.root);q.put('index','',now=0)
        c=Coordinator(self.root,clock=lambda:self.clock[0],wall=lambda:100+self.clock[0])
        reports=iter([False,True,True]);alive=iter([True,True,True,False]);wakes=[]
        def sample():return {**self.report,'ready':next(reports),'observed_at_mono':self.clock[0]}
        result=module.run(q,coordinator=c,probe=sample,wake=lambda:wakes.append(self.clock[0]),
            alive=lambda:next(alive),wait=lambda n:self.clock.__setitem__(0,self.clock[0]+n),wall=lambda:self.clock[0])
        self.assertEqual(result['status'],'parent_closed');self.assertEqual(len(wakes),1)
        self.assertEqual(len(q.pending()),1)

    def test_due_laya_wake_is_woken_without_learning_admission(self):
        module=self.monitor();import runtime_work
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
        module=self.monitor();import runtime_work
        q=runtime_work.WakeQueue(self.root);alive=iter([True,False]);waits=[]
        result=module.run(q,probe=lambda:self.fail('idle probe'),wake=lambda:self.fail('idle worker'),alive=lambda:next(alive),wait=waits.append)
        self.assertEqual(result['samples'],0);self.assertEqual(waits,[30.])

    def test_shared_daemon_attaches_ticks_and_closes_monitor_with_its_lifetime(self):
        self.monitor();from context_learning import inference_service as service
        class StopServe(BaseException):pass
        class Socket:
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def bind(inner,path):Path(path).touch(mode=0o600)
            def listen(inner,*args):pass
            def settimeout(inner,*args):pass
            def accept(inner):raise StopServe()
        monitor=Mock(status='running');factory=Mock(return_value=monitor)
        with patch.object(service.socket,'socket',return_value=Socket()):
            with self.assertRaises(StopServe):service._serve(self.root,engine=Mock(),monitor_factory=factory)
        factory.assert_called_once_with(self.root)
        self.assertGreaterEqual(monitor.tick.call_count,1);monitor.close.assert_called_once()


if __name__=='__main__':unittest.main()
