"""Selected worker runtime lifetime with actual stores and kernel owner probes."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('worker_owner_seed',ROOT/'test/fixtures/laya-product-sync-worker-checks.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
from memvane.sync import product_sync_worker as worker; import memvane.journal.install_journal as worker_install_journal
from laya_index_runtime import LayaIndexRuntime
from memvane.runtime.sync_owner_lock import sync_owner
from memvane.wiki.intake import wiki_discovery as discovery
from memvane.resident import embed_resident as resident
from memvane.resident import resident_service as resident_service
from memvane.journal import install_journal as journal

PROBE=r'''
import json,signal,sys
from pathlib import Path
signal.alarm(5)
sys.path.insert(0,sys.argv[1])
from laya_index_sync import _owner_lock
with _owner_lock(Path(sys.argv[2])) as owned:print(json.dumps(dict(available=owned)),flush=True)
'''

HOLDER=r'''
import json,signal,sys
from pathlib import Path
signal.alarm(20)
sys.path.insert(0,sys.argv[1])
from memvane.runtime.sync_owner_lock import sync_owner
with sync_owner(Path(sys.argv[2])) as owner:
    print(json.dumps(dict(owned=owner is not None)),flush=True)
    if owner is not None:assert sys.stdin.readline()=='RELEASE\n'
'''


class WorkerOwnerChecks(unittest.TestCase):
    @contextmanager
    def held_owner(self,queue):
        import select
        child=subprocess.Popen([sys.executable,'-B','-I','-c',HOLDER,str(ROOT),str(queue)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        try:
            if not select.select([child.stdout],[],[],6)[0]:raise RuntimeError('owner readiness timeout')
            self.assertTrue(json.loads(child.stdout.readline())['owned'])
            yield
        finally:
            try:out,err=child.communicate('RELEASE\n',timeout=4)
            except subprocess.TimeoutExpired:
                child.kill();child.communicate();raise
            self.assertEqual(child.returncode,0,err)

    def cold_default(self,fixture):
        from contextlib import ExitStack
        stack=ExitStack()
        self.addCleanup(stack.close)
        patches=[mock.patch.object(worker,'_selected',side_effect=lambda _p,**_k:(fixture.decision,fixture.proof())),
            mock.patch.object(worker,'select_route',return_value=fixture.decision),
            mock.patch.object(journal,'journal_version',return_value=4),
            mock.patch.object(worker,'ReviewedLocalQmdProvider',fixture.provider_class()),
            mock.patch.object(worker,'resource_report',return_value={'ready':True}),
            mock.patch.object(worker_install_journal,'verify_selected_route_for_write',side_effect=lambda _d:fixture.proof()),
            mock.patch.object(resident,'probe',return_value='absent'),
            mock.patch.object(resident,'read_owner_state',return_value=None),
            mock.patch.object(discovery,'observe_grants',return_value=None)]
        for patch in patches:stack.enter_context(patch)
        return stack
    def fixture(self,capacity):
        cls=seed.V2CapacityWorkerChecks if capacity else seed.V2WorkerChecks
        fixture=cls('runTest');self.addCleanup(fixture.doCleanups);fixture.setUp()
        if capacity:fixture.stores()
        return fixture

    def available(self,queue):
        result=subprocess.run([sys.executable,'-B','-I','-c',PROBE,str(ROOT),str(queue)],
            encoding='utf8',capture_output=True,timeout=6)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)['available']

    def lifetime(self,capacity):
        fixture=self.fixture(capacity);events=[];active={'provider':False}
        original_activity=seed.Coordinator.activity
        @contextmanager
        def activity(controller,lane,*args):
            with original_activity(controller,lane,*args) as admitted:
                active['provider']=True
                try:yield admitted
                finally:active['provider']=False
        outer=self
        class Runtime(LayaIndexRuntime):
            def __init__(self,*args,**kwargs):
                events.append(dict(phase='runtime-constructor',provider=active['provider'],
                    ownerAvailable=outer.available(fixture.queue_path)))
                super().__init__(*args,**kwargs)
            def close(self,**kwargs):
                events.append(dict(phase='runtime-close-before',provider=active['provider'],
                    ownerAvailable=outer.available(fixture.queue_path)))
                super().close(**kwargs)
                events.append(dict(phase='runtime-close-after',provider=active['provider'],
                    ownerAvailable=outer.available(fixture.queue_path)))
        def drain(_decision,_grants,*,recheck):
            recheck()
            events.append(dict(phase='discovery-after-sync',provider=active['provider'],
                ownerAvailable=self.available(fixture.queue_path)))
            return {'state':'complete'}
        with mock.patch.object(worker,'LayaIndexRuntime',Runtime), \
             mock.patch.object(seed.Coordinator,'activity',activity), \
             mock.patch.object(discovery,'observe_grants',return_value={'grants':{}}), \
             mock.patch.object(discovery,'drain_after_sync',side_effect=drain):
            result=fixture.run_capacity()[0] if capacity else fixture.run_worker()
        self.assertEqual(result['state'],'indexed')
        print(json.dumps(dict(case='actual-selected-worker-owner-lifetime',capacity=capacity,
            events=events,modelActuallyStarted=False)),flush=True)
        self.assertEqual([e['phase'] for e in events],['runtime-constructor',
            'runtime-close-before','runtime-close-after','discovery-after-sync'])
        for event in events[:3]:
            self.assertTrue(event['provider']);self.assertFalse(event['ownerAvailable'])
        self.assertFalse(events[-1]['provider']);self.assertTrue(events[-1]['ownerAvailable'])

    def test_full_worker_owns_queue_before_runtime_until_close_and_releases_before_discovery(self):
        self.lifetime(False)

    def test_capacity_worker_owns_queue_before_runtime_until_close_and_releases_before_discovery(self):
        self.lifetime(True)

    def test_busy_capacity_owner_defers_before_runtime_or_embedding(self):
        fixture=self.fixture(True)
        with sync_owner(fixture.queue_path) as owner:
            self.assertIsNotNone(owner)
            with mock.patch.object(worker,'LayaIndexRuntime',wraps=LayaIndexRuntime) as constructor:
                result,started=fixture.run_capacity()
        print(json.dumps(dict(case='actual-worker-busy-owner',result=result,
            runtimeConstructions=constructor.call_count,serviceStarts=started)),flush=True)
        self.assertEqual(result,{'state':'deferred','reason':'owner_busy'})
        self.assertEqual(constructor.call_count,0);self.assertEqual(started,0)
        self.assertEqual(fixture.embedded,[])

    def test_default_cold_resident_busy_owner_never_requests_adoption(self):
        fixture=self.fixture(True);controller=seed.Coordinator();controller.root=fixture.home
        with self.cold_default(fixture),self.held_owner(fixture.queue_path), \
             mock.patch.object(resident_service,'request_adopt') as adopt, \
             mock.patch.object(worker,'LayaIndexRuntime',wraps=LayaIndexRuntime) as constructor:
            result=worker.run(fixture.home,coordinator=controller)
        print(json.dumps(dict(case='default-cold-busy-owner',result=result,
            adoptionRequests=adopt.call_count,runtimeConstructions=constructor.call_count)),flush=True)
        self.assertEqual(result,{'state':'deferred','reason':'owner_busy'})
        adopt.assert_not_called();constructor.assert_not_called()

    def test_default_cold_resident_requests_under_owner_then_releases_without_provider_wait(self):
        fixture=self.fixture(True);controller=seed.Coordinator();controller.root=fixture.home
        observed=[]
        def adopt(*_args,**_kwargs):observed.append(self.available(fixture.queue_path))
        with self.cold_default(fixture),mock.patch.object(resident_service,'request_adopt',side_effect=adopt), \
             mock.patch.object(worker,'LayaIndexRuntime',wraps=LayaIndexRuntime) as constructor:
            result=worker.run(fixture.home,coordinator=controller)
        free=self.available(fixture.queue_path)
        print(json.dumps(dict(case='default-cold-request-owner',result=result,
            ownerAvailableDuringAdopt=observed,ownerAvailableAfterDefer=free,
            providerAdmissions=len(controller.lanes))),flush=True)
        self.assertEqual(result,{'state':'deferred','reason':'laya_service_preparing'})
        self.assertEqual(observed,[False]);self.assertTrue(free)
        self.assertEqual(controller.lanes,[]);constructor.assert_not_called()

    def test_owner_briefly_held_by_another_process_is_awaited_not_deferred(self):
        # The resident service re-proves its bindings under this per-UID owner
        # for a second or two; the off-hook worker waits instead of deferring.
        import select
        import laya_index_sync as sync
        fixture=self.fixture(True)
        child=subprocess.Popen([sys.executable,'-B','-I','-c',HOLDER,str(ROOT),str(fixture.queue_path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf8')
        attempts=[];original=sync._sync_anchor
        @contextmanager
        def anchor():
            with original() as held:
                attempts.append(held is not None)
                if held is None and attempts.count(False)==1:
                    child.stdin.write('RELEASE\n');child.stdin.flush()  # Holder lets go only now.
                yield held
        def drain(_decision,_grants,*,recheck):
            recheck();return {'state':'complete'}
        try:
            if not select.select([child.stdout],[],[],6)[0]:raise RuntimeError('owner readiness timeout')
            self.assertTrue(json.loads(child.stdout.readline())['owned'])
            with mock.patch.object(sync,'_sync_anchor',anchor), \
                 mock.patch.object(discovery,'observe_grants',return_value={'grants':{}}), \
                 mock.patch.object(discovery,'drain_after_sync',side_effect=drain):
                result=fixture.run_capacity()[0]
        finally:
            if attempts.count(False)==0:child.stdin.write('RELEASE\n');child.stdin.flush()
            try:_out,err=child.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                child.kill();child.communicate();raise
        print(json.dumps(dict(case='worker-awaits-brief-owner',result=result.get('state'),
            busyAttempts=attempts.count(False),acquired=attempts.count(True))),flush=True)
        self.assertEqual(child.returncode,0,err)
        self.assertEqual(result['state'],'indexed',result)
        self.assertGreaterEqual(attempts.count(False),1);self.assertGreaterEqual(attempts.count(True),1)
        self.assertEqual(attempts[0],False)


if __name__=='__main__':unittest.main(verbosity=2)
