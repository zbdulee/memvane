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
sys.path.insert(0,str(ROOT/'core'))
spec=importlib.util.spec_from_file_location('worker_owner_seed',ROOT/'test/fixtures/laya-product-sync-worker-checks.py')
seed=importlib.util.module_from_spec(spec);spec.loader.exec_module(seed)
import laya_product_sync_worker as worker
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import sync_owner
import laya_wiki_discovery as discovery

PROBE=r'''
import json,signal,sys
from pathlib import Path
signal.alarm(5)
sys.path.insert(0,sys.argv[1])
from laya_index_sync import _owner_lock
with _owner_lock(Path(sys.argv[2])) as owned:print(json.dumps(dict(available=owned)),flush=True)
'''


class WorkerOwnerChecks(unittest.TestCase):
    def fixture(self,capacity):
        cls=seed.V2CapacityWorkerChecks if capacity else seed.V2WorkerChecks
        fixture=cls('runTest');self.addCleanup(fixture.doCleanups);fixture.setUp()
        if capacity:fixture.stores()
        return fixture

    def available(self,queue):
        result=subprocess.run([sys.executable,'-B','-I','-c',PROBE,str(ROOT/'core'),str(queue)],
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


if __name__=='__main__':unittest.main(verbosity=2)
