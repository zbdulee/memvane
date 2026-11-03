"""Actual full/capacity sync calls explicitly borrow a pre-open owner lease."""
from contextlib import ExitStack
import importlib.util
import inspect
import json
from pathlib import Path
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


class BorrowChecks(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
