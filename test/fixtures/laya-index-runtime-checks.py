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
from laya_index_source_port import scan, scan_collections
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

    def collection_roots(self):
        alpha = self.root / 'alpha'
        beta = self.root / 'beta'
        (alpha / 'nested').mkdir(parents=True)
        beta.mkdir()
        (alpha / 'one.md').write_text('silver otter', encoding='utf-8')
        (beta / 'two.md').write_text('purple finch', encoding='utf-8')
        return alpha, beta

    def test_invalid_collection_roots_fail_before_any_store_access(self):
        alpha, beta = self.collection_roots()
        link = self.root / 'link'
        link.symlink_to(beta)
        many = {}
        for i in range(17):
            path = self.root / f'many-{i}'
            path.mkdir()
            many[f'c{i}'] = str(path)
        invalid = {
            'empty': {},
            'nested': {'alpha': str(alpha), 'inner': str(alpha / 'nested')},
            'nested-reversed': {'inner': str(alpha / 'nested'), 'alpha': str(alpha)},
            'equal': {'alpha': str(alpha), 'again': str(alpha)},
            'filesystem-root': {'top': '/', 'beta': str(beta)},
            'relative': {'alpha': 'alpha'},
            'trailing-slash': {'alpha': str(alpha) + '/'},
            'dot-segment': {'alpha': str(alpha / 'nested' / '..')},
            'symlink': {'beta': str(link)},
            'missing': {'gone': str(self.root / 'gone')},
            'file': {'file': str(alpha / 'one.md')},
            'hidden-name': {'.wiki': str(alpha)},
            'slash-name': {'a/b': str(alpha)},
            'non-str-name': {1: str(alpha)},
            'non-str-root': {'alpha': alpha},
            'too-many': many,
        }
        def state():
            return (sorted(path.name for path in self.root.iterdir()),
                    self.db.read_bytes(), self.queue.read_bytes())
        before = state()
        for label, roots in invalid.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(IndexContractError, 'invalid_runtime_binding'):
                    self.runtime(source_root=roots)
        # Validation precedes opening either store: no bytes, sidecars or lock.
        self.assertEqual(state(), before)
        self.assertFalse(Path(str(self.queue) + '.sync.lock').exists())

    def test_collection_binding_is_frozen_and_enqueues_namespaced_manifest(self):
        alpha, beta = self.collection_roots()
        roots = {'beta': str(beta), 'alpha': str(alpha)}
        with self.runtime(source_root=roots) as runtime:
            roots['alpha'] = str(alpha / 'nested')
            roots['extra'] = str(self.source)
            self.assertEqual(list(runtime.source_root.items()),
                             [('alpha', str(alpha)), ('beta', str(beta))])
            with self.assertRaises(TypeError):
                runtime.source_root['alpha'] = str(self.source)
            manifest = scan_collections(runtime.source_root, runtime.scope,
                                        runtime.index).manifest
            self.assertNotEqual(manifest, scan(str(alpha), runtime.scope,
                                               runtime.index).manifest)
            job = dict(claim='claim-c', scope=runtime.scope, gen='generation-c',
                       source_manifest=manifest, claim_epoch=1, base_gen=None,
                       claim_digest=payload_digest(runtime.scope, 'generation-c',
                                                   manifest, 1, None))
            self.assertEqual(runtime.enqueue_claim(job), 'pending')
            self.assertEqual(sync_full(runtime, before_publish=lambda: None,
                                       require_vector=False)['state'], 'indexed')
            self.assertEqual([row['path'] for row in runtime.query('"otter"', 10)],
                             ['alpha/one.md'])
            (beta / 'two.md').write_text('changed finch', encoding='utf-8')
            stale = dict(job, claim='claim-d')
            with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                runtime.enqueue_claim(stale)
            self.assertIsNone(runtime.queue.get('claim-d'))


if __name__ == '__main__':
    unittest.main()
