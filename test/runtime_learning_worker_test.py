"""Durable owned learning workers: synthetic only, no teacher/model/OS probe.

The hook persists a full-worker request, or a learning wake asks for a
training-only retry; the runtime dispatcher starts one owned child and returns
at once. Temporary roots only; children are tiny Python processes that hold
the inherited lease and exit on stdin EOF.
"""
from contextlib import contextmanager
import fcntl
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core'))
from context_learning import shared
from runtime_lease import atomic_json

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

    def drain(self,queue,execute,wall,max_runs=2):
        runtime=importlib.import_module('runtime_work')
        clock=[0.]
        return runtime.drain(queue,execute=execute,ready=lambda:True,clock=lambda:clock[0],
            wall=lambda:wall[0],sleep=lambda dt:clock.__setitem__(0,clock[0]+dt),max_runs=max_runs)

    @contextmanager
    def handler_env(self,train=no_inline_training):
        import config,setup_guard
        from context_learning import history_lifecycle
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(setup_guard,'status',return_value={'status':'ready'}), \
             patch.object(history_lifecycle,'worker_lease',no_lease), \
             patch.object(shared,'run_training_pending',side_effect=train), \
             patch.object(shared.subprocess,'Popen',side_effect=self.spawn):
            yield

    def test_launch_persists_request_and_wake_without_direct_popen(self):
        self.assertNotIn('Popen',inspect.getsource(shared.launch))
        with patch.object(shared,'_policy',return_value={'checked':True}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(shared,'_reconcile_source_cards'),patch.object(shared.job_telemetry,'emit'), \
             patch.object(shared.subprocess,'Popen',side_effect=AssertionError('direct worker spawn')), \
             patch('runtime_work.request',return_value={'status':'queued'}) as wake:
            self.assertEqual(shared.launch({},self.project),{'status':'queued'})
            first=json.loads((self.shared/shared.FULL_WORKER_REQUEST).read_text())
            self.assertEqual(shared.launch({},self.project,after_index=True),{'status':'queued'})
        second=shared._full_worker_request(self.shared)
        self.assertNotEqual(first['nonce'],second['nonce']);self.assertTrue(second['afterIndex'])
        self.assertEqual(wake.call_count,2)
        wake.assert_called_with('learning',str(self.project.resolve()))

    def test_restart_reruns_full_worker_once_while_index_keeps_priority(self):
        runtime=importlib.import_module('runtime_work')
        # After a reboot: one counted start that never recorded an outcome.
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':1,'outcome':None,'acked':False})
        q=runtime.WakeQueue(self.wakes);q.put('learning',str(self.project),now=0)
        calls=[];wall=[1.]
        def execute(row):
            calls.append(row['kind'])
            return True if row['kind']=='index' else runtime.dispatch(row)
        with self.handler_env():
            self.drain(q,execute,wall)
            self.assertEqual(len(self.spawned),1);self.assertTrue(self.lease_busy())
            argv,kw=self.spawned[0]
            self.assertEqual(argv[3:],['run-owned-worker',str(self.project.resolve()),str(kw['pass_fds'][0])])
            self.assertEqual(self.state()['starts'],2)
            # The child is still active: index runs first and learning neither
            # blocks the dispatcher nor starts a second owned child.
            q.put('index','',now=0);wall[0]=1000;calls.clear()
            self.assertEqual(self.drain(q,execute,wall)['runs'],2)
            self.assertEqual(calls,['index','learning']);self.assertEqual(len(self.spawned),1)
            self.assertEqual([row['kind'] for row in q.pending()],['learning'])
            # The owned child records its outcome, then exits: one ACK, no rerun.
            atomic_json(self.shared/shared.FULL_WORKER_STATE,
                {'nonce':NONCE,'starts':2,'outcome':'drained','acked':False})
            self.reap();self.assertFalse(self.lease_busy())
            wall[0]=5000;self.drain(q,execute,wall)
        self.assertEqual(len(self.spawned),1);self.assertEqual(q.pending(),[])
        self.assertTrue(self.state()['acked'])

    def test_training_retry_runs_in_owned_child_while_index_dispatches(self):
        runtime=importlib.import_module('runtime_work')
        q=runtime.WakeQueue(self.wakes);q.put('learning',str(self.project),now=0)
        calls=[];wall=[1.]
        def execute(row):
            calls.append(row['kind'])
            return True if row['kind']=='index' else runtime.dispatch(row)
        with self.handler_env():
            self.drain(q,execute,wall)
            self.assertEqual(len(self.spawned),1);self.assertTrue(self.lease_busy())
            argv,kw=self.spawned[0]
            self.assertEqual(argv[3:],['run-owned-training',str(self.project.resolve()),str(kw['pass_fds'][0])])
            self.assertNotIn('QMD_JOB_ID',kw['env'])
            state=self.training_state();revision=q.pending()[0]['revision']
            self.assertEqual((state['starts'],state['outcome'],state['revision']),(1,None,revision))
            # Concurrent index wake while training runs: it dispatches first,
            # and the learning wake neither blocks nor starts a second child.
            q.put('index','',now=0);wall[0]=1000;calls.clear()
            self.assertEqual(self.drain(q,execute,wall)['runs'],2)
            self.assertEqual(calls,['index','learning']);self.assertEqual(len(self.spawned),1)
            self.assertEqual([row['kind'] for row in q.pending()],['learning'])
            # The child settles the revision it was started for, then exits.
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**state,'outcome':'staged'})
            self.reap();self.assertFalse(self.lease_busy())
            wall[0]=5000;self.drain(q,execute,wall)
        self.assertEqual(len(self.spawned),1);self.assertEqual(q.pending(),[])
        self.assertTrue(self.training_state()['acked'])

    def test_full_settled_request_falls_back_to_owned_training_child(self):
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':1,'outcome':'drained','acked':True})
        import runtime_handlers
        with self.handler_env():
            self.assertFalse(runtime_handlers.dispatch(self.row(3)))
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.spawned[0][0][3],'run-owned-training')

    def test_training_child_crash_restarts_are_bounded(self):
        import runtime_handlers
        with self.handler_env():
            for starts in range(1,shared.MAX_FULL_WORKER_STARTS+1):
                self.assertFalse(runtime_handlers.dispatch(self.row(4)))
                self.assertEqual(self.training_state()['starts'],starts)
                self.reap()   # Crash: the child exits without an outcome.
            self.assertTrue(runtime_handlers.dispatch(self.row(4)))
        self.assertEqual(len(self.spawned),shared.MAX_FULL_WORKER_STARTS)
        self.assertEqual(self.training_state()['outcome'],'abandoned_after_restarts')

    def test_wake_after_settled_training_coalesces_into_one_new_run(self):
        runtime=importlib.import_module('runtime_work')
        q=runtime.WakeQueue(self.wakes);q.put('learning',str(self.project),now=0)
        import runtime_handlers
        with self.handler_env():
            first=q.pending()[0]
            self.assertFalse(runtime_handlers.dispatch(first))
            # A wake re-put during the run gets a newer revision.
            q.put('learning',str(self.project),now=0);q.put('learning',str(self.project),now=0)
            second=q.pending()[0];self.assertGreater(second['revision'],first['revision'])
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**self.training_state(),'outcome':'waiting'})
            self.reap()
            self.assertFalse(runtime_handlers.dispatch(second))
            self.assertEqual(len(self.spawned),2);self.assertEqual(self.training_state()['starts'],1)
            atomic_json(self.shared/shared.TRAINING_WORKER_STATE,{**self.training_state(),'outcome':'waiting'})
            self.reap()
            self.assertTrue(runtime_handlers.dispatch(second))
        self.assertEqual(len(self.spawned),2)

    def test_malformed_records_are_quarantined_and_acked_without_spawn(self):
        import runtime_handlers
        with self.handler_env():
            atomic_json(self.shared/shared.FULL_WORKER_REQUEST,{'schema':'wrong'})
            self.assertTrue(runtime_handlers.dispatch(self.row(1)))
            self.assertTrue((self.shared/(shared.FULL_WORKER_REQUEST+'.invalid')).exists())
            self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
                {'nonce':NONCE,'starts':'1','outcome':None,'acked':False})
            self.assertTrue(runtime_handlers.dispatch(self.row(1)))
            self.assertEqual(self.state()['outcome'],'abandoned_invalid_state')
            self.training_request(revision=-1)
            self.assertTrue(runtime_handlers.dispatch(self.row(1)))
            self.assertTrue((self.shared/(shared.TRAINING_WORKER_STATE+'.invalid')).exists())
            self.assertEqual(self.spawned,[])
            # Nothing re-reads a quarantined record: the next wake starts fresh.
            self.assertFalse(runtime_handlers.dispatch(self.row(2)))
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.spawned[0][0][3],'run-owned-training')

    def test_crash_loop_is_bounded_and_acknowledged(self):
        self.request();atomic_json(self.shared/shared.FULL_WORKER_STATE,
            {'nonce':NONCE,'starts':shared.MAX_FULL_WORKER_STARTS,'outcome':None,'acked':False})
        with self.handler_env():
            import runtime_handlers
            self.assertTrue(runtime_handlers.dispatch(self.row(1)))
        self.assertEqual(self.spawned,[]);self.assertEqual(self.state()['outcome'],'abandoned_after_restarts')

    def owned(self,result):
        """Run the full child body once with a real inherited-style lease FD."""
        fd=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        import config
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(shared,'run',side_effect=result) as worker, \
             patch('runtime_work.request') as wake:
            answer=shared._run_owned_worker(str(self.project),str(fd))
        self.assertFalse(self.lease_busy())
        return answer,worker,wake

    def owned_training(self,result,*,setup='ready'):
        """Run the training child body once; the teacher path must stay unused."""
        fd=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR|os.O_CREAT,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        import config,setup_guard
        with patch.object(config,'find_project_config',return_value={'config':{}}), \
             patch.object(shared,'_safe_root',return_value=self.shared), \
             patch.object(setup_guard,'status',return_value={'status':setup}), \
             patch.object(shared,'run',side_effect=AssertionError('teacher path entered')), \
             patch.object(shared,'run_training_pending',side_effect=result) as train, \
             patch('runtime_work.request',return_value={'status':'queued'}) as wake:
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
        import runtime_handlers
        with self.handler_env():
            self.assertFalse(runtime_handlers.dispatch(self.row(5)))
        self.assertEqual([argv[3] for argv,_ in self.spawned],['run-owned-training'])
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
            import config
            with patch.object(config,'find_project_config',return_value={'config':{}}), \
                 patch.object(shared,'_safe_root',return_value=self.shared), \
                 patch.object(shared,'run',side_effect=AssertionError('duplicate owner')), \
                 patch.object(shared,'run_training_pending',side_effect=AssertionError('duplicate owner')):
                with self.assertRaises(BlockingIOError):shared._run_owned_worker(str(self.project),str(stray))
                stray=os.open(self.shared/shared.FULL_WORKER_LOCK,os.O_RDWR)
                with self.assertRaises(BlockingIOError):shared._run_owned_training(str(self.project),str(stray))
        finally:os.close(holder)


class WakeQueueChecks(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name).resolve()/'wakes';root.mkdir(mode=0o700)
        self.runtime=importlib.import_module('runtime_work')
        self.queue=self.runtime.WakeQueue(root)

    def test_revisions_stay_unique_after_ack_and_recreate(self):
        self.queue.put('learning','/p',now=0);first=self.queue.pending()[0]['revision']
        with self.queue.connect() as db:db.execute('DELETE FROM wakes')
        self.queue.put('learning','/p',now=0)
        self.assertGreater(self.queue.pending()[0]['revision'],first)

    def test_capacity_deferred_wake_is_retained_then_promoted(self):
        import runtime_wake_policy
        with patch.object(self.runtime,'MAX_WAKES',3),patch.object(runtime_wake_policy,'MAX_OVERFLOW',1):
            self.assertEqual(self.queue.offer('learning','/a',now=0),'queued')
            self.assertEqual(self.queue.offer('learning','/b',now=0),'queued')
            self.assertEqual(self.queue.offer('learning','/c',now=0),'retained')
            self.assertEqual(self.queue.offer('learning','/c',now=1),'retained')
            self.assertEqual(self.queue.offer('learning','/d',now=0),'dropped')
            self.assertEqual(sorted(row['target'] for row in self.queue.pending()),['/a','/b'])
            with self.queue.connect() as db:db.execute("DELETE FROM wakes WHERE target='/a'")
            self.assertEqual(sorted(row['target'] for row in self.queue.pending()),['/b','/c'])
            with self.queue.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM wake_overflow').fetchone()[0],0)


if __name__=='__main__':unittest.main()
