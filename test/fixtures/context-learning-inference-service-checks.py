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


class PathTests(unittest.TestCase):
    def test_root_is_shared_under_home_and_private(self):
        with tempfile.TemporaryDirectory(prefix='qmd-svc-path-') as temp:
            home=Path(temp).resolve()
            with patch.object(Path,'home',return_value=home):
                root=service.service_root(create=True)
                self.assertEqual(root,service.service_root())
                self.assertEqual(root.stat().st_mode&0o777,0o700)
                self.assertIn('qmd-auto-context',str(root))
                root.chmod(0o755)
                with self.assertRaises(ValueError):service.service_root()

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
                kwargs={'engine':engine,'validate':fake_validate,'ready':ready,'capacity':1})
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
            time.sleep(.12);release.set()
            self.assertEqual(service.resident._read(first)['status'],'ok');first.close()
            self.assertEqual(service.resident._read(waiting)['status'],'deadline_expired');waiting.close()
            self.assertEqual(observed,['blocking'])
            self.assertEqual(exchange(root,envelope(namespace='other'))['prediction']['namespace'],'other')
            self.assertEqual(exchange(root,{'command':'shutdown'})['status'],'stopped')
            thread.join(2);self.assertFalse(thread.is_alive())
            self.assertFalse((root/service.SOCKET_NAME).exists())

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
                self.assertEqual(exchange(root,{'command':'shutdown'})['status'],'stopped')
                self.assertEqual(children[-1].wait(timeout=3),0)
            finally:
                for child in children:
                    if child.poll() is None:
                        try:exchange(root,{'command':'shutdown'})
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
