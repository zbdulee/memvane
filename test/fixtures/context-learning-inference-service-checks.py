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
                self.assertIn('qmd-auto-context',str(root))
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
                with patch.object(service,'_signature',return_value='0'*64):
                    with self.assertRaisesRegex(service.ServiceFailure,'version_mismatch'):
                        start(0)
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


def fake_worker(root):
    def load(request):
        path=root/'synthetic-load-count'
        path.write_text(str(int(path.read_text())+1) if path.exists() else '1')
        return request['generation'][-1]
    def validate(env):
        if env.get('synthetic_crash') is True:os._exit(75)
        return fake_validate(env)
    engine=service.GenerationEngine(load=load,
        forward=lambda loaded,request,**kw:{'head':loaded,'namespace':request['namespace']},
        admission=admit,rss=lambda:100)
    service._serve(root,engine=engine,validate=validate)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':fake_worker(Path(sys.argv[2]))
    else:unittest.main()
