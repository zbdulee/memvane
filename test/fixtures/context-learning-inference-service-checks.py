"""Synthetic object engine and real local socket/lease contracts; never ML."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
assert importlib.util.find_spec('context_learning.inference_service'), 'shared_inference_service_missing'
from context_learning import inference_service as service


def job(head='a', namespace='one', base='base'):
    return {'family': ('runtime', base, 'view'), 'generation': ('runtime', base, 'view', head),
            'namespace': namespace, 'policy': {'maxRssMib': 1024},
            'request': {'synthetic': head}, 'deadline': time.monotonic()+5}


def admit(*_args, **_kwargs):
    return {'max_batch_size': 1, 'max_padded_tokens': 4096}, {
        'available_mib': 4096, 'ready': True}


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.loads=[]
        def load(request):
            self.loads.append(request['generation']);return request['generation'][-1]
        self.engine=service.GenerationEngine(load=load,
            forward=lambda loaded, request, **kw: {'head': loaded, 'namespace': request['namespace']},
            admission=admit, rss=lambda: 100)

    def run_job(self, request, **kwargs):
        return self.engine.run(request, cancelled=lambda: False,
                               revalidate=lambda: request, **kwargs)[0]

    def test_two_namespaces_share_one_loaded_generation(self):
        self.assertEqual(self.run_job(job(namespace='one'))['namespace'], 'one')
        self.assertEqual(self.run_job(job(namespace='two'))['namespace'], 'two')
        self.assertEqual(len(self.loads),1)

    def test_explicit_prepare_keeps_incumbent_until_bound_activation(self):
        self.assertTrue(hasattr(self.engine,'prepare'),'candidate prepare missing')
        self.run_job(job());old=self.engine.active
        candidate=job('b');candidate['apply_id']='apply-b';candidate['expected_old']=job()['generation']
        token=self.engine.prepare(candidate,cancelled=lambda:False,revalidate=lambda:candidate)
        self.assertEqual(self.engine.active,old)
        self.assertEqual(self.run_job(job())['head'],'a')
        wrong={**candidate,'namespace':'other'}
        with self.assertRaises(service.ServiceFailure):self.engine.activate(token,wrong)
        self.engine.activate(token,candidate)
        self.assertEqual(self.run_job(job('b'))['head'],'b')
        self.engine.activate(token,candidate) # Lost ACK is idempotent.

    def test_prepare_failure_or_resource_loss_never_mutates_incumbent(self):
        self.assertTrue(hasattr(self.engine,'prepare'),'candidate prepare missing')
        self.run_job(job());old=self.engine.active
        candidate=job('b');candidate['apply_id']='apply-b';candidate['expected_old']=job()['generation']
        self.engine.admission=lambda *a,**kw:(None,{})
        with self.assertRaises(service.ServiceFailure):self.engine.prepare(candidate,cancelled=lambda:False,revalidate=lambda:candidate)
        self.assertEqual(self.engine.active,old)

    def test_unknown_rss_at_activation_keeps_old_model(self):
        self.run_job(job());old=self.engine.active
        candidate=job('b');candidate['apply_id']='apply-b';candidate['expected_old']=job()['generation']
        token=self.engine.prepare(candidate,cancelled=lambda:False,revalidate=lambda:candidate)
        self.engine.rss=lambda:float('nan')
        with self.assertRaises(service.ServiceFailure):self.engine.activate(token,candidate)
        self.assertEqual(self.engine.active,old)

    def test_failed_replacement_preserves_incumbent(self):
        self.run_job(job())
        def broken(_request):raise RuntimeError('private body must not cross wire')
        self.engine.load=broken
        with self.assertRaisesRegex(service.ServiceFailure,'replacement_failed'):
            self.run_job(job('b'))
        self.assertEqual(self.run_job(job())['head'],'a')

    def test_replacement_published_only_after_final_validation(self):
        self.run_job(job())
        candidate=job('b')
        with self.assertRaisesRegex(service.ServiceFailure,'request_changed'):
            self.engine.run(candidate,cancelled=lambda:False,revalidate=lambda:job('c'))
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(self.run_job(job('b'))['head'],'b')

    def test_first_candidate_forward_failure_does_not_publish(self):
        self.run_job(job())
        original=self.engine.forward
        def checked(loaded,request,**kwargs):
            if loaded=='b':raise RuntimeError('synthetic validation failure')
            return original(loaded,request,**kwargs)
        self.engine.forward=checked
        with self.assertRaises(RuntimeError):self.run_job(job('b'))
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(len(self.loads),2)

    def test_warm_forward_revocation_is_rechecked(self):
        self.run_job(job());request=job()
        def revoked():raise service.ServiceFailure('inference_attestation_required')
        with self.assertRaisesRegex(service.ServiceFailure,'inference_attestation_required'):
            self.engine.run(request,cancelled=lambda:False,revalidate=revoked)

    def test_warm_forward_memory_growth_is_rejected(self):
        self.run_job(job());rss=[100]
        original=self.engine.forward
        def grow(loaded,request,**kwargs):
            rss[0]=2048;return original(loaded,request,**kwargs)
        self.engine.rss=lambda:rss[0];self.engine.forward=grow
        with self.assertRaisesRegex(service.ServiceFailure,'resource_deferred'):
            self.run_job(job())

    def test_memory_recovers_after_failed_candidate(self):
        self.run_job(job());rss=[100];self.engine.rss=lambda:rss[0]
        original=self.engine.load
        def spike(request):rss[0]=2048;return original(request)
        self.engine.load=spike
        with self.assertRaisesRegex(service.ServiceFailure,'replacement_deferred'):
            self.run_job(job('b'))
        rss[0]=100
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(len(self.loads),2)

    def test_different_family_rejected_without_loading(self):
        self.run_job(job())
        with self.assertRaisesRegex(service.ServiceFailure,'model_family_mismatch'):
            self.run_job(job(base='different'))
        self.assertEqual(len(self.loads),1)

    def test_swap_without_overlap_headroom_keeps_incumbent(self):
        self.run_job(job())
        self.engine.admission=lambda *_args,**kw: ({'max_batch_size':1}, {'available_mib':100})
        with self.assertRaisesRegex(service.ServiceFailure,'replacement_deferred'):
            self.run_job(job('b'))
        self.assertEqual(len(self.loads),1)
        self.engine.admission=admit
        self.assertEqual(self.run_job(job())['head'],'a')

    def test_expired_or_cancelled_request_never_loads(self):
        request=job();request['deadline']=time.monotonic()-1
        with self.assertRaisesRegex(service.ServiceFailure,'deadline_expired'):
            self.run_job(request)
        with self.assertRaisesRegex(service.ServiceFailure,'deadline_expired'):
            self.engine.run(job(),cancelled=lambda:True,revalidate=lambda:job())
        self.assertFalse(self.loads)

    def test_scope_or_deadline_revocation_after_load_does_not_publish(self):
        self.run_job(job());request=job('b')
        def revoked():raise service.ServiceFailure('inference_attestation_required')
        with self.assertRaisesRegex(service.ServiceFailure,'inference_attestation_required'):
            self.engine.run(request,cancelled=lambda:False,revalidate=revoked)
        self.assertEqual(self.run_job(job())['head'],'a')


class WarmFirstLoadTests(unittest.TestCase):
    """A first load survives its request's deadline; nothing else changes."""
    def setUp(self):
        self.loads=[]
        def load(request):
            self.loads.append(request['generation']);return request['generation'][-1]
        self.engine=service.GenerationEngine(load=load,
            forward=lambda loaded, request, **kw: {'head': loaded},
            admission=admit, rss=lambda: 100)

    def run_job(self, request, **kwargs):
        return self.engine.run(request, cancelled=lambda: False,
                               revalidate=lambda: request, **kwargs)[0]

    def test_first_load_stays_warm_when_the_request_expires(self):
        for expiry in (ValueError('prediction_cancelled'), ValueError('resident_deadline_expired')):
            self.setUp()
            def late_forward(loaded, request, **kw):raise expiry
            self.engine.forward=late_forward
            with self.assertRaises(ValueError):self.run_job(job())
            self.assertEqual(service._failure(expiry),'deadline_expired')
            self.engine.forward=lambda loaded, request, **kw: {'head': loaded}
            self.assertEqual(self.run_job(job())['head'],'a')
            self.assertEqual(len(self.loads),1,'expired first load was discarded')

    def test_revalidation_out_of_time_keeps_the_first_load_warm(self):
        from context_learning.adapter_binding import BindingTimeout
        request=job();calls=[]
        def late():
            calls.append(1);raise BindingTimeout('trainer_timeout')
        with self.assertRaises(BindingTimeout) as caught:
            self.engine.run(request,cancelled=lambda:False,revalidate=late)
        self.assertEqual(service._failure(caught.exception),'deadline_expired')
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(len(self.loads),1)

    def test_expired_first_load_is_dropped_when_memory_grew_or_is_unknown(self):
        for grown in (2048, float('nan')):
            self.setUp();rss=[100];self.engine.rss=lambda:rss[0]
            def late(loaded, request, **kw):
                rss[0]=grown;raise ValueError('prediction_cancelled')
            self.engine.forward=late
            with self.assertRaises(ValueError):self.run_job(job())
            self.assertIsNone(self.engine.active,'over-budget model retained after expiry')

    def test_active_model_over_rss_is_released_then_reloaded(self):
        rss=[100];self.engine.rss=lambda:rss[0]
        self.assertEqual(self.run_job(job())['head'],'a')
        rss[0]=2048
        with self.assertRaisesRegex(service.ServiceFailure,'resource_deferred'):self.run_job(job())
        self.assertIsNone(self.engine.active,'over-budget model kept loaded')
        rss[0]=100
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(len(self.loads),2)

    def test_other_first_load_failures_and_replacements_publish_nothing(self):
        def broken(loaded, request, **kw):raise RuntimeError('synthetic forward failure')
        self.engine.forward=broken
        with self.assertRaises(RuntimeError):self.run_job(job())
        self.assertIsNone(self.engine.active)
        self.engine.forward=lambda loaded, request, **kw: {'head': loaded}
        self.run_job(job())
        def late(loaded, request, **kw):raise ValueError('prediction_cancelled')
        self.engine.forward=late
        with self.assertRaises(ValueError):self.run_job(job('b'))
        self.engine.forward=lambda loaded, request, **kw: {'head': loaded}
        self.assertEqual(self.run_job(job())['head'],'a')
        self.assertEqual(len(self.loads),3)


class ResponseTests(unittest.TestCase):
    def test_invalid_candidate_response_is_not_a_publishable_generation(self):
        from context_learning import laya_adapter
        request={'request':{'cases':[{'request_id':'r','input_sha256':'a',
                                    'candidates':[{'id':'wiki'}]}]}}
        with patch.object(laya_adapter,'predict_loaded',return_value={
                'schema_version':1,'predictions':[{'request_id':'r',
                'input_sha256':'a','selected_ids':['outside-project']}]}):
            with self.assertRaisesRegex(service.ServiceFailure,'invalid_prediction'):
                service._forward((None,None,{}),request,limits={},timing={},cancelled=lambda:False)


class MemoryObservationTests(unittest.TestCase):
    def test_current_rss_is_not_a_historical_peak(self):
        import resource
        from types import SimpleNamespace
        with patch.object(resource,'getrusage',return_value=SimpleNamespace(ru_maxrss=10**18)):
            current=service._rss_mib()
        self.assertGreater(current,0)
        self.assertLess(current,100000,'historical peak poisoned current admission')


class ProbeDeadlineTests(unittest.TestCase):
    def test_metadata_subprocess_uses_remaining_deadline(self):
        from context_learning import runtime_setup
        def child(*args,**kwargs):
            self.assertLessEqual(kwargs['timeout'],.2)
            raise subprocess.TimeoutExpired(args[0],kwargs['timeout'])
        with patch.object(runtime_setup.subprocess,'run',side_effect=child):
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime_setup._run_probe(sys.executable,deadline=time.monotonic()+.2)


class PathTests(unittest.TestCase):
    def test_root_is_shared_under_home_and_private(self):
        with tempfile.TemporaryDirectory(prefix='qmd-svc-path-') as temp:
            home=Path(temp).resolve()
            with patch.object(Path,'home',return_value=home):
                root=service.service_root(create=True,home=home)
                self.assertEqual(root,service.service_root(home=home))
                self.assertEqual(root.stat().st_mode&0o777,0o700)
                self.assertIn('memvane',str(root))
                root.chmod(0o755)
                with self.assertRaises(ValueError):service.service_root(home=home)

    def test_namespace_is_canonical_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory(prefix='qmd-svc-path-') as temp:
            state=Path(temp).resolve();state.chmod(0o700)
            self.assertEqual(service.namespace(state),hashlib.sha256(str(state).encode()).hexdigest())
            link=state/'alias';link.symlink_to(state,target_is_directory=True)
            with self.assertRaises(ValueError):service.namespace(link)


def exchange(root,envelope,timeout=3):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout);conn.connect(str(root/service.SOCKET_NAME))
        service.resident._write(conn,envelope)
        return service.resident._read(conn)


def shutdown_idle(root):
    # An RPC response is visible before score() finishes task_done(). Keep
    # the busy guard and wait only for that real transition, within the same
    # bounded fixture budget; never widen product deadlines or kill a worker.
    deadline=time.monotonic()+3
    while True:
        left=deadline-time.monotonic()
        if left<=0:raise AssertionError('owned fixture service did not become idle')
        answer=exchange(root,{'command':'shutdown'},timeout=left)
        if answer=={'status':'stopped'}:return answer
        if answer!={'status':'busy'}:raise AssertionError(answer)
        time.sleep(min(.005,left))


def envelope(head='a',namespace='one',seconds=3):
    now=time.time()
    return {'head':head,'namespace':namespace,'queued_at_epoch':now,'deadline_epoch':now+seconds}


def fake_validate(env):
    until,_=service.resident._request_window(env,now_epoch=time.time(),now_monotonic=time.monotonic())
    request=job(env['head'],env['namespace']);request['deadline']=until
    return request


class ServerTests(unittest.TestCase):
    def test_apply_commands_prepare_activate_recover_and_yield_to_inference(self):
        from context_learning.resource_coordination import Coordinator
        with tempfile.TemporaryDirectory(prefix='qmd-a-',dir='/tmp') as temp:
            root=Path(temp).resolve();root.chmod(0o700)
            tokens=[]
            for restart in range(2):
                ready=threading.Event()
                engine=service.GenerationEngine(load=lambda request:request['generation'][-1],
                    forward=lambda loaded,request,**kw:{'head':loaded},admission=admit,rss=lambda:100)
                def validate_apply(env):
                    request=fake_validate(env);request['apply_id']='synthetic-apply'
                    request['expected_old']=job()['generation'];return request
                thread=threading.Thread(target=service._serve,args=(root,),kwargs={
                    'engine':engine,'validate':fake_validate,'apply_validate':validate_apply,'ready':ready},daemon=True)
                thread.start();self.assertTrue(ready.wait(2))
                try:
                    if not restart:self.assertEqual(exchange(root,envelope())['status'],'ok')
                    control={**envelope(head='b'),'command':'apply_prepare'}
                    with Coordinator(root).inference(time.monotonic()+1,'priority'):
                        self.assertEqual(exchange(root,control)['status'],'resource_deferred')
                        self.assertEqual(exchange(root,{**control,'command':'apply_activate'})['status'],'resource_deferred')
                    prepared=exchange(root,control);self.assertEqual(prepared['status'],'prepared');tokens.append(prepared['token'])
                    if not restart:self.assertEqual(exchange(root,envelope())['prediction']['head'],'a')
                    activated=exchange(root,{**control,'command':'apply_activate'})
                    self.assertEqual(activated['status'],'activated');self.assertEqual(activated['token'],prepared['token'])
                    self.assertEqual(exchange(root,envelope(head='b'))['prediction']['head'],'b')
                finally:shutdown_idle(root);thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(tokens[0],tokens[1])

    def test_dispatcher_exists(self):
        self.assertTrue(hasattr(service,'_serve'),'bounded_dispatcher_missing')

    def test_queue_is_bounded_handshake_responsive_and_expired_work_skipped(self):
        self.assertTrue(hasattr(service,'_serve'),'bounded_dispatcher_missing')
        with tempfile.TemporaryDirectory(prefix='qmd-s-',dir='/tmp') as temp:
            root=Path(temp).resolve();root.chmod(0o700)
            scoring=threading.Event();release=threading.Event();ready=threading.Event()
            observed=[]
            def forward(loaded,request,**kwargs):
                observed.append(request['namespace'])
                if request['namespace']=='blocking':scoring.set();release.wait(3)
                return {'head':loaded,'namespace':request['namespace']}
            engine=service.GenerationEngine(load=lambda request:request['generation'][-1],
                forward=forward,admission=admit,rss=lambda:100)
            thread=threading.Thread(target=service._serve,args=(root,),
                kwargs={'engine':engine,'validate':fake_validate,'ready':ready,'capacity':1},daemon=True)
            thread.start();self.assertTrue(ready.wait(2))
            first=socket.socket(socket.AF_UNIX);first.settimeout(3);first.connect(str(root/service.SOCKET_NAME))
            service.resident._write(first,envelope(namespace='blocking'))
            self.assertTrue(scoring.wait(2))
            waiting=socket.socket(socket.AF_UNIX);waiting.settimeout(3);waiting.connect(str(root/service.SOCKET_NAME))
            service.resident._write(waiting,envelope(namespace='expired',seconds=.1))
            until=time.monotonic()+1
            while exchange(root,{'command':'version'})['queued']!=1 and time.monotonic()<until:time.sleep(.01)
            self.assertEqual(exchange(root,envelope(namespace='overflow'))['status'],'queue_full')
            self.assertTrue(exchange(root,{'command':'version'})['busy'])
            self.assertEqual(exchange(root,{'command':'shutdown'})['status'],'busy')
            time.sleep(.12);release.set()
            self.assertEqual(service.resident._read(first)['status'],'ok');first.close()
            self.assertEqual(service.resident._read(waiting)['status'],'deadline_expired');waiting.close()
            self.assertEqual(observed,['blocking'])
            self.assertEqual(exchange(root,envelope(namespace='other'))['prediction']['namespace'],'other')
            # A disconnected waiting caller cannot consume model work.
            release.clear();scoring.clear()
            blocking=socket.socket(socket.AF_UNIX);blocking.settimeout(3);blocking.connect(str(root/service.SOCKET_NAME))
            service.resident._write(blocking,envelope(namespace='blocking'))
            self.assertTrue(scoring.wait(2))
            abandoned=socket.socket(socket.AF_UNIX);abandoned.connect(str(root/service.SOCKET_NAME))
            service.resident._write(abandoned,envelope(namespace='abandoned'));abandoned.close()
            until=time.monotonic()+1
            while exchange(root,{'command':'version'})['queued']!=1 and time.monotonic()<until:time.sleep(.01)
            release.set();self.assertEqual(service.resident._read(blocking)['status'],'ok');blocking.close()
            until=time.monotonic()+1
            while exchange(root,{'command':'version'})['queued'] and time.monotonic()<until:time.sleep(.01)
            self.assertNotIn('abandoned',observed)
            self.assertEqual(shutdown_idle(root)['status'],'stopped')
            thread.join(2);self.assertFalse(thread.is_alive())
            self.assertFalse((root/service.SOCKET_NAME).exists())

    def test_over_budget_candidate_is_input_budget_not_worker_error(self):
        # A wiki card longer than the selector's token budget is refused by
        # the adapter. The hosted worker and the client keep that fixed code.
        from contextlib import nullcontext
        from context_learning import resource_coordination
        class NoCoordinator:
            def __init__(self,_root):pass
            def inference(self,*_args):return nullcontext()
        for message in ('model_token_budget_exceeded','tokenizer_model_limit_exceeded'):
            with tempfile.TemporaryDirectory(prefix='qmd-b-',dir='/tmp') as temp:
                root=Path(temp).resolve();root.chmod(0o700);ready=threading.Event()
                def forward(loaded,request,**kwargs):raise ValueError(message)
                engine=service.GenerationEngine(load=lambda request:request['generation'][-1],
                    forward=forward,admission=admit,rss=lambda:100)
                thread=threading.Thread(target=service._serve,args=(root,),
                    kwargs={'engine':engine,'validate':fake_validate,'ready':ready},daemon=True)
                thread.start();self.assertTrue(ready.wait(2))
                try:
                    self.assertEqual(exchange(root,envelope())['status'],'input_budget_exceeded')
                    with patch.object(service,'_make_envelope',side_effect=lambda *a:envelope()), \
                            patch.object(service,'service_root',return_value=root), \
                            patch.object(resource_coordination,'Coordinator',NoCoordinator), \
                            patch.object(service,'_ensure',return_value=None):
                        with self.assertRaises(service.ServiceFailure) as caught:
                            service.predict(root,{'trainerArgv':[sys.executable]},{},'0'*64,
                                            deadline=time.monotonic()+3)
                    self.assertEqual(str(caught.exception),'resident_service_input_budget_exceeded')
                finally:shutdown_idle(root);thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_invalid_wire_and_peer_failure_do_not_echo_body(self):
        with tempfile.TemporaryDirectory(prefix='qmd-s-',dir='/tmp') as temp:
            root=Path(temp).resolve();root.chmod(0o700);ready=threading.Event()
            def denied(_env):raise ValueError('synthetic-private-secret/body')
            thread=threading.Thread(target=service._serve,args=(root,),
                kwargs={'validate':denied,'ready':ready},daemon=True)
            thread.start();self.assertTrue(ready.wait(2))
            try:
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(1);conn.connect(str(root/service.SOCKET_NAME))
                    conn.sendall(b'invalid-json-private-secret\n')
                    reply=service.resident._read(conn)
                    self.assertEqual(reply['status'],'worker_error')
                    self.assertNotIn('private-secret',json.dumps(reply))
                self.assertNotIn('synthetic-private-secret',json.dumps(exchange(root,envelope())))
                with patch.object(service.resident,'_same_owner',return_value=False):
                    # Peer rejection precedes wire parsing. Sending after the
                    # server closes races BrokenPipe; read the refusal directly
                    # and prove no request body is needed to reject the peer.
                    with socket.socket(socket.AF_UNIX) as conn:
                        conn.settimeout(1);conn.connect(str(root/service.SOCKET_NAME))
                        self.assertEqual(service.resident._read(conn)['status'],'request_rejected')
            finally:
                shutdown_idle(root);thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_shutdown_waits_for_response_bookkeeping(self):
        with tempfile.TemporaryDirectory(prefix='qmd-s-',dir='/tmp') as temp:
            root=Path(temp).resolve();root.chmod(0o700)
            ready=threading.Event();bookkeeping=threading.Event();release=threading.Event()
            original_queue=service.queue.Queue
            class PausedQueue(original_queue):
                def task_done(self):
                    bookkeeping.set();release.wait(2);super().task_done()
            engine=service.GenerationEngine(load=lambda request:'a',
                forward=lambda loaded,request,**kw:{'head':loaded},admission=admit,rss=lambda:100)
            with patch.object(service.queue,'Queue',PausedQueue):
                thread=threading.Thread(target=service._serve,args=(root,),
                    kwargs={'engine':engine,'validate':fake_validate,'ready':ready},daemon=True)
                thread.start();self.assertTrue(ready.wait(2))
                try:
                    self.assertEqual(exchange(root,envelope())['status'],'ok')
                    self.assertTrue(bookkeeping.wait(1))
                    # A completed response must not bypass in-flight bookkeeping.
                    self.assertEqual(exchange(root,{'command':'shutdown'})['status'],'busy')
                    timer=threading.Timer(.05,release.set);timer.start()
                    self.assertEqual(shutdown_idle(root)['status'],'stopped')
                    timer.join(1)
                finally:
                    release.set()
                    if thread.is_alive():
                        try:shutdown_idle(root)
                        except (FileNotFoundError,ConnectionRefusedError,OSError):pass
                    thread.join(2)

    def test_startup_race_and_crash_recovery_use_one_kernel_lease(self):
        self.assertTrue(hasattr(service,'_ensure'),'singleton_startup_missing')
        with tempfile.TemporaryDirectory(prefix='qmd-s-',dir='/tmp') as temp:
            root=Path(temp).resolve();root.chmod(0o700);children=[];lock=threading.Lock()
            def spawn(_argv,**kwargs):
                child=subprocess.Popen([sys.executable,__file__,'--worker',str(root)],
                    env={**os.environ,'QMD_RECALL_LOG':'','PYTHONDONTWRITEBYTECODE':'1'},
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                with lock:children.append(child)
                return child
            def start(_):return service._ensure(root,sys.executable,time.monotonic()+3,spawn=spawn)
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(start,range(6)))
                self.assertEqual(len(children),1)
                self.assertEqual(exchange(root,envelope(namespace='claude'))['prediction']['head'],'a')
                self.assertEqual(exchange(root,envelope(namespace='codex'))['prediction']['head'],'a')
                self.assertEqual((root/'synthetic-load-count').read_text(),'1')
                try:exchange(root,{**envelope(),'synthetic_crash':True})
                except (OSError,ValueError):pass
                self.assertEqual(children[0].wait(timeout=3),75)
                start(0);self.assertEqual(len(children),2)
                self.assertEqual(exchange(root,envelope())['status'],'ok')
                self.assertEqual(shutdown_idle(root)['status'],'stopped')
                self.assertEqual(children[-1].wait(timeout=3),0)
            finally:
                for child in children:
                    if child.poll() is None:
                        try:shutdown_idle(root)
                        except Exception:child.terminate()
                    child.wait(timeout=3)
                    child.stderr.close()


class StaleServiceReplacementTests(unittest.TestCase):
    """A service from another bundle is replaced only while idle."""
    OLD='1'*64

    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='qmd-r-',dir='/tmp');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.root.chmod(0o700)
        self.children=[];self.addCleanup(self.reap)

    def reap(self):
        (self.root/'release').write_text('')
        for child in self.children:
            if child.poll() is None:
                try:shutdown_idle(self.root)
                except Exception:child.terminate()
            child.wait(timeout=5);child.stderr.close()

    def launch(self,**env):
        child=subprocess.Popen([sys.executable,__file__,'--worker',str(self.root)],
            env={**os.environ,'QMD_RECALL_LOG':'','PYTHONDONTWRITEBYTECODE':'1',**env},
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        self.children.append(child);return child

    def version(self):
        try:return exchange(self.root,{'command':'version'},timeout=1)
        except (FileNotFoundError,ConnectionRefusedError):return None

    def old_service(self,**env):
        child=self.launch(QMD_TEST_SERVICE_SIGNATURE=self.OLD,**env)
        deadline=time.monotonic()+5
        while self.version() is None:
            self.assertIsNone(child.poll(),'old fixture service exited')
            self.assertLess(time.monotonic(),deadline,'old fixture service never listened')
            time.sleep(.01)
        return child

    def spawn(self,_argv,**_kwargs):
        return self.launch()

    def ensure(self,seconds,*,spawn=None):
        return service._ensure(self.root,sys.executable,time.monotonic()+seconds,
                               spawn=spawn or self.spawn)

    def test_idle_old_signature_service_is_replaced_by_current(self):
        old=self.old_service()
        self.ensure(5)
        self.assertEqual(old.wait(timeout=3),0)  # graceful shutdown, not a signal
        self.assertEqual(len(self.children),2)
        self.assertTrue(service._ready(self.root,time.monotonic()+1))
        self.assertEqual(exchange(self.root,envelope())['status'],'ok')

    def test_busy_old_service_is_never_interrupted(self):
        old=self.old_service()
        answers=[]
        thread=threading.Thread(target=lambda:answers.append(exchange(self.root,
            {**envelope(seconds=8),'synthetic_block':True},timeout=10)))
        thread.start()
        deadline=time.monotonic()+5
        while not (self.version() or {}).get('busy'):
            self.assertLess(time.monotonic(),deadline,'fixture request never started')
            time.sleep(.01)
        with self.assertRaisesRegex(service.ServiceFailure,'starting'):
            self.ensure(1,spawn=lambda *_a,**_k:self.fail('spawned beside a busy service'))
        self.assertIsNone(old.poll())
        (self.root/'release').write_text('')
        thread.join(10)
        self.assertEqual(answers[0]['status'],'ok')
        self.ensure(5)
        self.assertEqual(old.wait(timeout=3),0)
        self.assertTrue(service._ready(self.root,time.monotonic()+1))

    def test_service_without_shutdown_is_terminated_only_when_verified(self):
        old=self.old_service(QMD_TEST_SERVICE_NO_SHUTDOWN='1')
        # Its command line is not this root's inference worker: left alone.
        with self.assertRaisesRegex(service.ServiceFailure,'starting'):
            self.ensure(2,spawn=lambda *_a,**_k:self.fail('spawned beside a live service'))
        self.assertIsNone(old.poll())
        worker=('python -I -B -c import runpy;runpy.run_module("context_learning.inference_service")'
                ' worker '+str(self.root))
        def command(pid):
            return (os.getuid(),worker) if pid==old.pid else None
        with patch.object(service,'_process_command',side_effect=command):
            self.ensure(5)
        self.assertEqual(old.wait(timeout=3),-15)
        self.assertEqual(len(self.children),2)
        self.assertTrue(service._ready(self.root,time.monotonic()+1))


def fake_worker(root):
    signature=os.environ.get('QMD_TEST_SERVICE_SIGNATURE')
    if signature:service._signature=lambda:signature
    if os.environ.get('QMD_TEST_SERVICE_NO_SHUTDOWN')=='1':
        read=service.resident._read
        def legacy(conn,**kw):
            row=read(conn,**kw)
            return {'command':'legacy-unknown'} if row=={'command':'shutdown'} else row
        service.resident._read=legacy
    def load(request):
        path=root/'synthetic-load-count'
        path.write_text(str(int(path.read_text())+1) if path.exists() else '1')
        return request['generation'][-1]
    def validate(env):
        if env.get('synthetic_crash') is True:os._exit(75)
        if env.pop('synthetic_block',None) is True:
            stop=time.monotonic()+9
            while not (root/'release').exists() and time.monotonic()<stop:time.sleep(.01)
        return fake_validate(env)
    engine=service.GenerationEngine(load=load,
        forward=lambda loaded,request,**kw:{'head':loaded,'namespace':request['namespace']},
        admission=admit,rss=lambda:100)
    service._serve(root,engine=engine,validate=validate)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':fake_worker(Path(sys.argv[2]))
    else:unittest.main()
