"""Durable owned learning workers: synthetic only, no teacher/model/OS probe.

The hook persists a full-worker request, or a learning wake asks for a
training-only retry; the runtime dispatcher starts one owned child and returns
at once. Temporary roots only; children are tiny Python processes that hold
the inherited lease and exit on stdin EOF.
"""
from contextlib import contextmanager
import fcntl
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from memvane.learning.context_learning import shared
from memvane.learning.context_learning.local_cycle import _atomic_json as atomic_json
REAL_POPEN=subprocess.Popen
NONCE='a'*32
JOB={key:'' for key in shared._FULL_WORKER_JOB_KEYS}
@contextmanager
def no_lease():yield
def no_inline_training(*args):
    raise AssertionError('training ran inline in the dispatcher')
class FullWorkerChecks(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'QMD_SETUP_GUARD_FIXTURE':'','QMD_SANDBOX':'','QMD_SHARED_GOLD_ONLY':''})
        env.start();self.addCleanup(env.stop)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        base=Path(temp.name).resolve();base.chmod(0o700)
        self.shared=base/'shared';self.shared.mkdir(mode=0o700)
        self.wakes=base/'wakes';self.wakes.mkdir(mode=0o700)
        self.project=base/'project';self.project.mkdir()
        self.children=[];self.spawned=[]
        self.addCleanup(self.reap)
    def reap(self):
        for child in self.children:
            if child.poll() is None:child.communicate(b'',timeout=5)
    def request(self,*,after_index=False):
        atomic_json(self.shared/shared.FULL_WORKER_REQUEST,{'schema':shared.FULL_WORKER_SCHEMA,
            'nonce':NONCE,'afterIndex':after_index,'job':JOB})
    def state(self):
        return json.loads((self.shared/shared.FULL_WORKER_STATE).read_text())
    def training_request(self,**changes):
        atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{'schema':shared.TRAINING_WORKER_SCHEMA,
            'nonce':NONCE,'revision':7,'starts':1,'outcome':None,'acked':False,**changes})
    def training_state(self):
        return json.loads((self.shared/shared.TRAINING_WORKER_STATE).read_text())
    def spawn(self,argv,**kw):
        # Stand-in for the owned worker: it only holds the inherited lease.
        self.spawned.append((argv,kw))
        child=REAL_POPEN([sys.executable,'-B','-c','import sys;sys.stdin.read()'],stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,pass_fds=kw['pass_fds'])
        self.children.append(child);return child
    def lease_busy(self):
        fd=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return True
            return False
        finally:os.close(fd)
    def row(self,revision):
        return {'kind':'learning','target':str(self.project),'revision':revision}
    def dispatch(self,row):
        """One learning wake as the app wake handler runs it: the learning step,
        acknowledged unless the owned child is running or was just started."""
        step=shared.learning_worker_step(self.shared,self.project,row['revision'])
        return step['status'] not in ('running','started')
    @contextmanager
    def handler_env(self,train=no_inline_training):
        from memvane.settings import api as config
        from memvane.learning.context_learning import history_lifecycle
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch('memvane.learning.context_learning.learning.ready',return_value=True), \
             patch.object(history_lifecycle,'worker_lease',no_lease), \
             patch.object(shared,'run_training_pending',side_effect=train), \
             patch.object(shared.subprocess,'Popen',side_effect=self.spawn):
            yield
    def test_launch_persists_request_and_wake_without_direct_popen(self):
        self.assertNotIn('Popen',inspect.getsource(shared.launch))
        with patch.object(shared,'_policy',return_value={'checked':True}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(shared,'telemetry_emit'), \
             patch.object(shared.subprocess,'Popen',side_effect=AssertionError('direct worker spawn')), \
             patch('memvane.runtime.api.request',return_value={'status':'queued'}) as wake:
            self.assertEqual(shared.launch({},self.project),{'status':'queued'})
            first=json.loads((self.shared/shared.FULL_WORKER_REQUEST).read_text())
            self.assertEqual(shared.launch({},self.project,after_index=True),{'status':'queued'})
        second=shared._full_worker_request(self.shared)
        self.assertNotEqual(first['nonce'],second['nonce']);self.assertTrue(second['afterIndex'])
        self.assertEqual(wake.call_count,2)
        wake.assert_called_with('learning',str(self.project.resolve()))
    def test_restart_reruns_full_worker_once_while_the_child_runs(self):
        # After a reboot: one counted start that never recorded an outcome.
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':1,'outcome':None,'acked':False})
        with self.handler_env():
            self.assertFalse(self.dispatch(self.row(1)))
            self.assertEqual(len(self.spawned),1);self.assertTrue(self.lease_busy())
            argv,kw=self.spawned[0]
            self.assertEqual(argv[4:],['run-owned-worker',str(self.project.resolve()),str(kw['pass_fds'][0])])
            self.assertEqual(self.state()['starts'],2)
            # The child is still active: another wake neither blocks nor starts a second child.
            self.assertFalse(self.dispatch(self.row(2)));self.assertEqual(len(self.spawned),1)
            # The owned child records its outcome, then exits: one ACK, no rerun.
            atomic_json(self.shared/shared.FULL_WORKER_STATE,
                {'nonce':NONCE,'starts':2,'outcome':'drained','acked':False})
            self.reap();self.assertFalse(self.lease_busy())
            self.assertTrue(self.dispatch(self.row(2)))
        self.assertEqual(len(self.spawned),1)
        self.assertTrue(self.state()['acked'])
    def test_training_retry_runs_in_owned_child_while_the_wake_stays(self):
        with self.handler_env():
            self.assertFalse(self.dispatch(self.row(3)))
            self.assertEqual(len(self.spawned),1);self.assertTrue(self.lease_busy())
            argv,kw=self.spawned[0]
            self.assertEqual(argv[4:],['run-owned-training',str(self.project.resolve()),str(kw['pass_fds'][0])])
            self.assertNotIn('QMD_JOB_ID',kw['env'])
            state=self.training_state()
            self.assertEqual((state['starts'],state['outcome'],state['revision']),(1,None,3))
            # A wake while training runs neither blocks nor starts a second child.
            self.assertFalse(self.dispatch(self.row(3)));self.assertEqual(len(self.spawned),1)
            # The child settles the revision it was started for, then exits.
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**state,'outcome':'staged'})
            self.reap();self.assertFalse(self.lease_busy())
            self.assertTrue(self.dispatch(self.row(3)))
        self.assertEqual(len(self.spawned),1)
        self.assertTrue(self.training_state()['acked'])
    def test_full_settled_request_falls_back_to_owned_training_child(self):
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':1,'outcome':'drained','acked':True})
        with self.handler_env():
            self.assertFalse(self.dispatch(self.row(3)))
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.spawned[0][0][4],'run-owned-training')
    def test_training_child_crash_restarts_are_bounded(self):
        with self.handler_env():
            for starts in range(1,shared.MAX_FULL_WORKER_STARTS+1):
                self.assertFalse(self.dispatch(self.row(4)))
                self.assertEqual(self.training_state()['starts'],starts)
                self.reap()   # Crash: the child exits without an outcome.
            self.assertTrue(self.dispatch(self.row(4)))
        self.assertEqual(len(self.spawned),shared.MAX_FULL_WORKER_STARTS)
        self.assertEqual(self.training_state()['outcome'],'abandoned_after_restarts')
    def test_wake_after_settled_training_coalesces_into_one_new_run(self):
        from memvane.runtime import api as runtime
        q=runtime.wake_queue(self.wakes);q.put('learning',str(self.project),now=0)
        with self.handler_env():
            first=q.pending()[0]
            self.assertFalse(self.dispatch(first))
            # A wake re-put during the run gets a newer revision.
            q.put('learning',str(self.project),now=0);q.put('learning',str(self.project),now=0)
            second=q.pending()[0];self.assertGreater(second['revision'],first['revision'])
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**self.training_state(),'outcome':'waiting'})
            self.reap()
            self.assertFalse(self.dispatch(second))
            self.assertEqual(len(self.spawned),2);self.assertEqual(self.training_state()['starts'],1)
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**self.training_state(),'outcome':'waiting'})
            self.reap()
            self.assertTrue(self.dispatch(second))
        self.assertEqual(len(self.spawned),2)
    def test_malformed_records_are_quarantined_and_acked_without_spawn(self):
        with self.handler_env():
            atomic_json(self.shared/shared.FULL_WORKER_REQUEST,{'schema':'wrong'})
            self.assertTrue(self.dispatch(self.row(1)))
            self.assertTrue((self.shared/(shared.FULL_WORKER_REQUEST+'.invalid')).exists())
            self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
                {'nonce':NONCE,'starts':'1','outcome':None,'acked':False})
            self.assertTrue(self.dispatch(self.row(1)))
            self.assertEqual(self.state()['outcome'],'abandoned_invalid_state')
            self.training_request(revision=-1)
            self.assertTrue(self.dispatch(self.row(1)))
            self.assertTrue((self.shared/(shared.TRAINING_WORKER_STATE+'.invalid')).exists())
            self.assertEqual(self.spawned,[])
            # Nothing re-reads a quarantined record: the next wake starts fresh.
            self.assertFalse(self.dispatch(self.row(2)))
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.spawned[0][0][4],'run-owned-training')
    def test_crash_loop_is_bounded_and_acknowledged(self):
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':shared.MAX_FULL_WORKER_STARTS,'outcome':None,'acked':False})
        with self.handler_env():
                self.assertTrue(self.dispatch(self.row(1)))
        self.assertEqual(self.spawned,[]);self.assertEqual(self.state()['outcome'],'abandoned_after_restarts')
    def owned(self,result):
        """Run the full child body once with a real inherited-style lease FD."""
        fd=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        from memvane.settings import api as config
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(shared,'run',side_effect=result) as worker, \
             patch('memvane.runtime.api.request') as wake:
            answer=shared._run_owned_worker(str(self.project),str(fd))
        self.assertFalse(self.lease_busy())
        return answer,worker,wake
    def owned_training(self,result,*,setup='ready'):
        """Run the training child body once; the teacher path must stay unused."""
        fd=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        from memvane.settings import api as config
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch('memvane.learning.context_learning.learning.ready',return_value=setup=='ready'), \
             patch.object(shared,'run',side_effect=AssertionError('teacher path entered')), \
             patch.object(shared,'run_training_pending',side_effect=result) as train, \
             patch('memvane.runtime.api.request',return_value={'status':'queued'}) as wake:
            answer=shared._run_owned_training(str(self.project),str(fd))
        self.assertFalse(self.lease_busy())
        return answer,train,wake
    def test_owned_child_runs_full_worker_once_and_settles(self):
        self.request(after_index=True)
        answer,worker,wake=self.owned(lambda *a,**kw:{'status':'drained'})
        self.assertEqual(answer['status'],'drained');worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs['wait_for_lock'],True);wake.assert_not_called()
        self.assertEqual(self.state()['outcome'],'drained')
    def test_owned_child_resource_deferral_keeps_request_pending(self):
        self.request()
        atomic_json(self.shared/shared.FULL_WORKER_STATE,{'nonce':NONCE,'starts':2,'outcome':None,'acked':False})
        answer,worker,wake=self.owned(lambda *a,**kw:{'status':'deferred_resource'})
        self.assertEqual(answer['learning_retry'],{'status':'durable_wake_pending'})
        wake.assert_called_once_with('learning',str(self.project.resolve()))
        self.assertEqual(self.state(),{'nonce':NONCE,'starts':0,'outcome':None,'acked':False})
    def test_full_deferral_after_teacher_hands_off_to_training_only(self):
        self.request()
        answer,worker,wake=self.owned(lambda *a,**kw:{'status':'deferred_resource',
            'teacher':{'status':'done'},'cycle':{'status':'deferred_resource'}})
        self.assertEqual(answer['learning_retry'],{'status':'training_only_handoff'})
        wake.assert_called_once_with('learning',str(self.project.resolve()))
        self.assertEqual(self.state()['outcome'],shared.TRAINING_HANDOFF)
        with self.handler_env():
            self.assertFalse(self.dispatch(self.row(5)))
        self.assertEqual([argv[4] for argv,_ in self.spawned],['run-owned-training'])
        self.assertTrue(self.state()['acked'])
    def test_finished_cycle_settles_without_handoff(self):
        self.request()
        self.owned(lambda *a,**kw:{'status':'staged','cycle':{'status':'staged'}})
        self.assertEqual(self.state()['outcome'],'staged')
    def test_training_child_settles_without_teacher(self):
        self.training_request()
        answer,train,wake=self.owned_training(lambda *a:{'status':'staged'})
        self.assertEqual(answer,{'status':'staged'})
        train.assert_called_once_with(self.shared,self.project.resolve());wake.assert_not_called()
        self.assertEqual((self.training_state()['outcome'],self.training_state()['acked']),('staged',False))
    def test_training_child_pressure_deferral_keeps_durable_wake(self):
        self.training_request(starts=2)
        answer,train,wake=self.owned_training(lambda *a:{'status':'deferred_resource'})
        self.assertEqual(answer['learning_retry'],{'status':'durable_wake_pending'})
        wake.assert_called_once_with('learning',str(self.project.resolve()))
        self.assertEqual((self.training_state()['starts'],self.training_state()['outcome']),(0,None))
    def test_training_child_rechecks_setup_before_training(self):
        self.training_request()
        answer,train,wake=self.owned_training(lambda *a:self.fail('trained during setup'),setup='install')
        self.assertEqual(answer['status'],'setup_in_progress');train.assert_not_called()
        wake.assert_called_once();self.assertIsNone(self.training_state()['outcome'])
    def test_stray_process_cannot_become_a_second_owner(self):
        self.request()
        holder=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(holder,fcntl.LOCK_EX)
        stray=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR)
        try:
            from memvane.settings import api as config
            with patch.object(config,'find_project_config',return_value={'config':{}}), \
                 patch.object(shared,'_safe_root',return_value=self.shared), \
                 patch.object(shared,'run',side_effect=AssertionError('duplicate owner')), \
                 patch.object(shared,'run_training_pending',side_effect=AssertionError('duplicate owner')):
                with self.assertRaises(BlockingIOError):shared._run_owned_worker(str(self.project),str(stray))
                stray=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR)
                with self.assertRaises(BlockingIOError):shared._run_owned_training(str(self.project),str(stray))
        finally:os.close(holder)



if __name__ == '__main__':
    unittest.main()
