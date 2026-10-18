#!/usr/bin/env python3
"""Disposable explicit-binding checks; no installed host or operating DB."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'core'))

from laya_index_a import IndexContractError, LayaIndexA
from laya_index_claim_queue import ClaimQueue, payload_digest
from laya_index_runtime import LayaIndexRuntime
from laya_index_source_port import scan
from laya_index_sync import sync_full


class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-runtime-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'chapter.md').write_text('silver otter', encoding='utf-8')
        self.db = self.root / 'index.sqlite3'
        self.queue = self.root / 'claims.sqlite3'
        index = LayaIndexA(self.db, create=True)
        self.incarnation = index.incarnation()
        index.close()
        ClaimQueue.create(self.queue, index_incarnation=self.incarnation).close()

    def runtime(self, **overrides):
        args = dict(source_root=str(self.source), scope='a' * 32,
                    db_path=str(self.db), queue_path=str(self.queue),
                    db_incarnation=self.incarnation)
        args.update(overrides)
        return LayaIndexRuntime(**args)

    def job(self, runtime):
        manifest = scan(str(self.source), runtime.scope, runtime.index).manifest
        return dict(claim='claim-1', scope=runtime.scope, gen='generation-1',
                    source_manifest=manifest, claim_epoch=1, base_gen=None,
                    claim_digest=payload_digest(runtime.scope, 'generation-1',
                                                manifest, 1, None))

    def test_exact_claim_publishes_and_searches_one_bound_scope(self):
        with self.runtime() as runtime:
            job = self.job(runtime)
            self.assertEqual(runtime.enqueue_claim(job), 'pending')
            with self.assertRaisesRegex(IndexContractError, 'selected_sync_only'):
                runtime.process_claim(job['claim'])
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       require_vector=False)['state'], 'indexed')
            rows = runtime.query('"otter"', 10)
            self.assertEqual([(row['scope'], row['path']) for row in rows],
                             [('a' * 32, 'chapter.md')])
            self.assertFalse(runtime.inspect()['deletion_enabled'])

    def test_wrong_database_incarnation_blocks_before_queue_open(self):
        with self.assertRaisesRegex(IndexContractError, 'database_incarnation_mismatch'):
            self.runtime(db_incarnation='0' * 32)
        with self.runtime() as runtime:
            self.assertEqual(runtime.index.incarnation(), self.incarnation)

    def test_unbound_queue_is_rejected(self):
        unbound = self.root / 'unbound.sqlite3'
        ClaimQueue.create(unbound).close()
        with self.assertRaisesRegex(IndexContractError, 'queue_index_binding_mismatch'):
            self.runtime(queue_path=str(unbound))

    def test_changed_source_cannot_enqueue_stale_manifest(self):
        with self.runtime() as runtime:
            job = self.job(runtime)
            (self.source / 'chapter.md').write_text('changed otter', encoding='utf-8')
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                runtime.enqueue_claim(job)
            self.assertIsNone(runtime.queue.get(job['claim']))

    def test_foreign_scope_claim_is_not_processed(self):
        with self.runtime() as runtime:
            job = self.job(runtime)
            job['scope'] = 'foreign-scope'
            with self.assertRaisesRegex(IndexContractError, 'claim_scope_mismatch'):
                runtime.enqueue_claim(job)
            with self.assertRaisesRegex(IndexContractError, 'selected_sync_only'):
                runtime.process_claim('missing')
            with self.assertRaisesRegex(IndexContractError, 'owner_death_proof_required'):
                runtime.process_claim('missing', recover=True)

    def test_exclusion_blocks_current_generation_without_partial_result(self):
        with self.runtime() as runtime:
            job = self.job(runtime)
            runtime.enqueue_claim(job)
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       require_vector=False)['state'], 'indexed')
            runtime.index.exclude(runtime.scope, path='chapter.md')
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                runtime.query('"otter"', 10)


if __name__ == '__main__':
    unittest.main()
