"""Actual full/capacity sync calls explicitly borrow a pre-open owner lease."""
from contextlib import ExitStack
import importlib.util
import inspect
import json
from pathlib import Path
import select
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))
import laya_index_sync as sync
from laya_index_a import IndexContractError


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'test/fixtures' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


full = load('borrow_full_fixture', 'laya-index-sync-checks.py')
capacity = load('borrow_capacity_fixture', 'laya-index-capacity-product-checks.py')

HOLDER = r'''
import json,signal,sys
from pathlib import Path
signal.alarm(15)
sys.path.insert(0,sys.argv[1])
from laya_index_sync import sync_owner
with sync_owner(Path(sys.argv[2])) as owner:
    print(json.dumps(dict(acquired=owner is not None)),flush=True)
    if owner is not None:assert sys.stdin.readline()=='RELEASE\n'
'''


class BorrowChecks(unittest.TestCase):
    def successor(self, queue, handles):
        child = subprocess.Popen([sys.executable, '-B', '-I', '-c', HOLDER,
            str(ROOT/'core'), str(queue)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding='utf8')
        def finish():
            try:out,err=child.communicate('RELEASE\n',timeout=4)
            except subprocess.TimeoutExpired:
                child.kill();child.communicate();raise
            self.assertEqual(child.returncode,0,err)
        handles.callback(finish)
        if not select.select([child.stdout],[],[],4)[0]:
            raise RuntimeError('fresh successor exceeded finite readiness wait')
        return json.loads(child.stdout.readline())['acquired']

    def complete_catalog(self, runtime):
        runtime.catalog.request_scan()
        for _ in range(128):
            if runtime.catalog.step(20,100000,100)['status']=='complete':return
        self.fail('bounded catalog prerequisite did not finish')

    def fixture(self, cls):
        fixture = cls('runTest')
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def reject_without_sql(self, runtime, call, claims):
        databases = [runtime.index.db, runtime.queue.db]
        if runtime.catalog is not None:
            databases += [runtime.catalog.db, runtime.binding_store.db]
        statements = []
        before = tuple(db.total_changes for db in databases)
        for db in databases:
            db.set_trace_callback(statements.append)
        try:
            for claim in claims:
                with self.assertRaises(IndexContractError):
                    call(claim)
            self.assertEqual(statements, [], 'invalid owner must fail before the first SQL')
            self.assertEqual(tuple(db.total_changes for db in databases), before)
        finally:
            for db in databases:
                db.set_trace_callback(None)

    def test_full_sync_explicit_preopen_owner_publishes_and_expired_or_forged_owner_runs_no_sql(self):
        fixture = self.fixture(full.SyncChecks)
        marks = []
        with ExitStack() as handles:
            with sync.sync_owner(fixture.queue_path) as claim:
                self.assertIsNotNone(claim)
                runtime = handles.enter_context(fixture.runtime())
                baseline = sync.sync_full(runtime, before_publish=lambda: marks.append('publish'),
                                          require_vector=False)
                self.assertEqual(baseline['reason'], 'owner_busy')
                self.assertEqual(marks, [])
                self.assertIn('owner', inspect.signature(sync.sync_full).parameters,
                              'explicit owner transfer is missing after real owner_busy baseline')
                result = sync.sync_full(runtime, before_publish=lambda: marks.append('publish'),
                                        require_vector=False, owner=claim)
                self.assertEqual(result['state'], 'indexed')
                self.assertEqual([row['path'] for row in runtime.query('"silver"', 5)], ['chapter.md'])
            self.assertTrue(marks)
            marks.clear()
            self.reject_without_sql(runtime, lambda owner: sync.sync_full(runtime,
                before_publish=lambda: marks.append('invalid'), require_vector=False, owner=owner),
                [claim, True, {}, object.__new__(type(claim))])
            self.assertEqual(marks, [])
            print(json.dumps(dict(case='full-explicit-sync-owner',published=True,
                unborrowedBusy=True,expiredAndForgedSql=0)), flush=True)

    def test_capacity_sync_explicit_preopen_owner_serves_bound_search_and_wrong_queue_runs_no_sql(self):
        fixture = self.fixture(capacity.CapacityProductCase)
        fixture.write(fixture.wiki, 'borrowed.md', '# Borrowed\nborrowneedle owner publication\n')
        with ExitStack() as handles:
            with sync.sync_owner(Path(fixture.queue_path)) as claim:
                self.assertIsNotNone(claim)
                runtime = handles.enter_context(fixture.runtime())
                baseline = fixture.step(runtime)
                self.assertEqual(baseline['reason'], 'owner_busy')
                self.assertEqual(fixture.marks, [])
                self.assertIn('owner', inspect.signature(sync.sync_step).parameters,
                              'capacity explicit owner transfer is missing after real owner_busy baseline')
                result = fixture.drive(runtime, owner=claim)[-1]
                self.assertEqual(result['state'], 'indexed')
                self.assertEqual(fixture.paths(fixture.query(runtime, 'borrowneedle')), ['wiki/borrowed.md'])
            self.assertTrue(fixture.marks)
            fixture.marks.clear()
            call = lambda owner: fixture.step(runtime, owner=owner)
            self.reject_without_sql(runtime, call, [claim, False])
            other = Path(fixture.queue_path).with_name('different-queue')
            other.write_bytes(b'private unrelated queue fixture');other.chmod(0o600)
            with sync.sync_owner(other) as wrong:
                self.assertIsNotNone(wrong)
                self.reject_without_sql(runtime, call, [wrong])
            self.assertEqual(fixture.marks, [])
            print(json.dumps(dict(case='capacity-explicit-sync-owner',boundSearch=True,
                unborrowedBusy=True,expiredAndWrongQueueSql=0)), flush=True)

    def test_before_publish_expiry_blocks_successor_and_all_subsequent_claim_or_head_writes(self):
        for kind,cls in (('full',full.SyncChecks),('capacity',capacity.CapacityProductCase)):
            with self.subTest(kind=kind):
                fixture=self.fixture(cls)
                if kind=='capacity':fixture.write(fixture.wiki,'expiry.md','synthetic expiry content')
                with ExitStack() as handles,ExitStack() as scope:
                    runtime=handles.enter_context(fixture.runtime())
                    if kind=='capacity':self.complete_catalog(runtime)
                    claim=scope.enter_context(sync.sync_owner(Path(fixture.queue_path)))
                    before=(runtime.index.db.total_changes,runtime.queue.db.total_changes)
                    successors=[]
                    def expire():
                        scope.close();successors.append(self.successor(fixture.queue_path,handles))
                    call=sync.sync_full if kind=='full' else sync.sync_step
                    with self.assertRaises(IndexContractError):
                        call(runtime,before_publish=expire,require_vector=False,owner=claim)
                    after=(runtime.index.db.total_changes,runtime.queue.db.total_changes)
                    print(json.dumps(dict(case='before-publish-expiry',kind=kind,
                        successorAcquired=successors,changes=[a-b for a,b in zip(after,before)])),flush=True)
                    self.assertEqual(after,before,'expired callback must not enqueue, stage, publish or ACK')
                    self.assertEqual(successors,[False],'borrow must retain exclusion until unwind')

    def test_before_head_expiry_preserves_head_and_never_acks_while_releasing_own_retry(self):
        fixture=self.fixture(capacity.CapacityProductCase)
        fixture.write(fixture.wiki,'head-expiry.md','synthetic head expiry content')
        with ExitStack() as handles,ExitStack() as scope:
            runtime=handles.enter_context(fixture.runtime());self.complete_catalog(runtime)
            claim=scope.enter_context(sync.sync_owner(Path(fixture.queue_path)))
            observed={}
            def expire(index,binding,job):
                observed.update(claim=job['claim'],indexChanges=index.db.total_changes)
                scope.close();observed['successorAcquired']=self.successor(fixture.queue_path,handles)
            with self.assertRaises(IndexContractError):
                sync.sync_step(runtime,before_publish=lambda:None,before_head_publish=expire,owner=claim)
            state=runtime.queue.get(observed['claim'])['state']
            heads=runtime.index.db.execute('SELECT gen FROM head').fetchall()
            print(json.dumps(dict(case='before-head-expiry',heads=heads,claimState=state,
                subsequentIndexChanges=runtime.index.db.total_changes-observed['indexChanges'],
                successorAcquired=observed['successorAcquired'])),flush=True)
            self.assertEqual(heads,[]);self.assertNotEqual(state,'acked')
            self.assertEqual(runtime.index.db.total_changes,observed['indexChanges'])
            self.assertFalse(observed['successorAcquired'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
