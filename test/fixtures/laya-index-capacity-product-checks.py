"""Disposable capacity product-path checks (sync_step / query_bound).

Synthetic temp-dir corpora, a temp A-int DB, a temp claim queue, a temp
catalog and a temp binding store only; no operating DB, source, queue, model
or network. Crash cases run a child Python process against the temp stores.
"""
from __future__ import annotations

from contextlib import closing
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_index_a
import laya_index_capacity as cap
import laya_source_catalog
from laya_index_a import IndexContractError, LayaIndexA, _StreamDigest, _digest
from laya_index_capacity import (GenerationBindingStore, capture_bound_generation_binding,
                                 enqueue_bound_generation, generation_digest,
                                 persist_generation_binding, publish_bound,
                                 stage_bound_streaming)
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import _owner_lock, sync_full, sync_step
from laya_source_catalog import LayaSourceCatalog

SCOPE = 'b' * 32
FILLER = 'lorem ipsum dolor sit amet '

CRASH_CHILD = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
point = sys.argv[2]
config = json.loads(sys.argv[3])
import laya_index_a, laya_index_capacity, laya_index_claim_queue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import sync_step

def die(*_args, **_kwargs):
    os._exit(70)

if point == 'after_persist':
    laya_index_capacity.enqueue_bound_generation = die
elif point == 'after_enqueue':
    laya_index_capacity.stage_bound_streaming = die
elif point == 'after_stage':
    laya_index_capacity.publish_bound = die
elif point == 'after_publish':
    laya_index_claim_queue.ClaimQueue.conditional_ack = die
elif point == 'mid_stage':
    real = laya_index_a.LayaIndexA.stage_stream
    def stage_stream(self, gen, scope, documents, *args, **kwargs):
        def dying():
            for n, document in enumerate(documents):
                if n == 3:
                    os._exit(70)
                yield document
        return real(self, gen, scope, dying(), *args, **kwargs)
    laya_index_a.LayaIndexA.stage_stream = stage_stream
with LayaIndexRuntime(**config) as runtime:
    for _ in range(1000):
        sync_step(runtime, before_publish=lambda: None)
os._exit(0)
'''


def sized(head, size):
    return (head + FILLER * (size // len(FILLER) + 1))[:size]


class CapacityProductCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='laya-capacity-product-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.plans = self.base / 'src' / 'plans'
        self.wiki = self.base / 'src' / 'wiki'
        self.plans.mkdir(parents=True)
        self.wiki.mkdir(parents=True)
        self.roots = {'plans': str(self.plans), 'wiki': str(self.wiki)}
        self.state = self.base / 'state'
        self.state.mkdir()
        self.db_path = str(self.state / 'index.sqlite')
        self.queue_path = str(self.state / 'queue.sqlite')
        self.catalog_path = str(self.state / 'catalog.sqlite')
        self.store_path = str(self.state / 'bindings.sqlite')
        index = LayaIndexA(self.db_path, create=True)
        self.incarnation = index.incarnation()
        index.close()
        ClaimQueue.create(self.queue_path, index_incarnation=self.incarnation).close()
        LayaSourceCatalog(self.catalog_path, self.roots, SCOPE, create=True).close()
        self.new_store(self.store_path)
        self.marks = []

    def new_store(self, path):
        GenerationBindingStore(path, scope=SCOPE, db_incarnation=self.incarnation,
                               roots=self.roots, create=True).close()

    def config(self, **change):
        return {'source_root': self.roots, 'scope': SCOPE, 'db_path': self.db_path,
                'queue_path': self.queue_path, 'db_incarnation': self.incarnation,
                'catalog_path': self.catalog_path,
                'binding_store_path': self.store_path, **change}

    def runtime(self, **change):
        runtime = LayaIndexRuntime(**self.config(**change))
        self.addCleanup(runtime.close)
        return runtime

    @staticmethod
    def write(root, rel, text):
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def step(self, runtime, **kw):
        return sync_step(runtime, before_publish=lambda: self.marks.append('fenced'), **kw)

    def drive(self, runtime, **kw):
        results = []
        for _ in range(10_000):
            results.append(self.step(runtime, **kw))
            if results[-1]['state'] != 'pending':
                return results
        raise AssertionError('sync_step did not settle')

    @staticmethod
    def head(runtime):
        row = runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',
                                       (SCOPE,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def members(runtime, gen):
        return [row[0] for row in runtime.index.db.execute(
            'SELECT path FROM membership WHERE gen=? ORDER BY path', (gen,))]

    @staticmethod
    def published(runtime):
        return runtime.index.db.execute(
            "SELECT COUNT(*) FROM generations WHERE state='published'").fetchone()[0]

    @staticmethod
    def paths(answer):
        return [row['path'] for row in answer['rows']]

    def query(self, runtime, term, **kw):
        return runtime.query_bound(f'"{term}"', 5, **kw)

    def small_corpus(self):
        self.write(self.plans, 'a.md', '# A\nalpha otter\n')
        self.write(self.plans, 'b.md', '# B\nbeta heron\n')
        self.write(self.wiki, 'c.md', '# C\ngamma finch\n')

    def corrupt_binding(self, gen):
        """Damage one stored row only; schema, meta and triggers stay exact."""
        with closing(sqlite3.connect(self.store_path, isolation_level=None)) as db:
            ddl = db.execute("SELECT sql FROM sqlite_master "
                             "WHERE name='binding_update_seal'").fetchone()[0]
            db.execute('BEGIN IMMEDIATE')
            db.execute('DROP TRIGGER binding_update_seal')
            self.assertEqual(db.execute("UPDATE bindings SET record='{}' WHERE generation=?",
                                        (gen,)).rowcount, 1)
            db.execute(ddl)
            db.execute('COMMIT')

    @staticmethod
    def pins(runtime):
        return runtime.index.db.execute('SELECT COUNT(*) FROM pins').fetchone()[0]


class StreamingStageTests(CapacityProductCase):
    def test_stream_digest_equals_manifest_digest(self):
        entries = [('plans/"q"\\x.md', 'a' * 64, 1), ('plans/b.md', 'b' * 64, 0),
                   ('wiki/한글/노트.md', 'c' * 64, 1)]
        for count in range(len(entries) + 1):
            digest = _StreamDigest(SCOPE)
            for entry in entries[:count]:
                digest.add(*entry)
            self.assertEqual(digest.hexdigest(), _digest(SCOPE, entries[:count]))

    def test_stage_stream_refuses_unsorted_mismatch_and_changed_replay(self):
        runtime = self.runtime()
        index = runtime.index
        docs = [('plans/a.md', 'alpha', True), ('plans/b.md', 'beta', False)]
        manifest = _digest(SCOPE, [(path, laya_index_a.hashlib.sha256(
            body.encode()).hexdigest(), int(flag)) for path, body, flag in docs])
        seal = {'base': None, 'claim_digest': 'd' * 64, 'claim_epoch': 1}
        count = lambda: index.db.execute('SELECT COUNT(*) FROM generations').fetchone()[0]
        with self.assertRaisesRegex(IndexContractError, 'invalid_document'):
            index.stage_stream('g-unsorted', SCOPE, iter(reversed(docs)),
                               source_manifest=manifest, **seal)
        with self.assertRaisesRegex(IndexContractError, 'manifest_mismatch'):
            index.stage_stream('g-mismatch', SCOPE, iter(docs),
                               source_manifest='0' * 64, **seal)
        self.assertEqual(count(), 0)  # both rolled back, nothing building
        self.assertEqual(index.stage_stream('g-s1', SCOPE, iter(docs),
                                            source_manifest=manifest, **seal), 'prepared')
        self.assertEqual(index.stage_stream('g-s1', SCOPE, iter(docs),
                                            source_manifest=manifest, **seal),
                         'already_staged')
        changed = [docs[0], ('plans/b.md', 'beta changed', False)]
        with self.assertRaisesRegex(IndexContractError, 'manifest_mismatch'):
            index.stage_stream('g-s1', SCOPE, iter(changed), source_manifest=manifest, **seal)
        with self.assertRaisesRegex(IndexContractError, 'generation_conflict'):
            index.stage_stream('g-s1', SCOPE, iter(docs), source_manifest=manifest,
                               base=None, claim_digest='e' * 64, claim_epoch=1)
        self.assertEqual(count(), 1)
        self.assertEqual(index.inspect()['generations'], 1)

    def test_large_corpus_bounded_steps_and_streamed_stage(self):
        size = 25_000
        for d in range(13):
            for f in range(20):
                n = d * 20 + f
                self.write(self.plans, f'part{d:02d}/doc{f:02d}.md',
                           sized(f'# Doc {n}\nmarker{n:04d}token\n', size))
        self.write(self.wiki, 'card.md', '# Card\nwiki side\n')
        total = 260 * size + len('# Card\nwiki side\n')
        self.assertGreater(total, 5_000_000)
        runtime = self.runtime()
        reads = []
        real_read = laya_source_catalog._read_exact

        def counted(*args):
            reads.append(args[2])
            return real_read(*args)

        def forbidden(*_args, **_kwargs):
            raise AssertionError('whole-corpus document list built')

        tracemalloc.start()
        try:
            with mock.patch.object(laya_source_catalog, '_read_exact', counted), \
                 mock.patch.object(LayaIndexA, 'stage', forbidden), \
                 mock.patch.object(laya_source_catalog, 'stage_from_catalog', forbidden):
                results = self.drive(runtime)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(results[-1]['state'], 'indexed')
        # 15 listings + 185 hashes fill the first step; the rest fit the second.
        self.assertGreaterEqual(len(results), 2)
        for result in results[:-1]:
            self.assertEqual((result['state'], result['reason']), ('pending', 'scan_pending'))
            self.assertLessEqual(result['work']['units'], 200)
            self.assertLessEqual(result['work']['bytes_read'], 5_000_000)
        # Every member was re-read once for stage; no body list was held.
        self.assertEqual(len(reads), 261)
        self.assertEqual(len(set(reads)), 261)
        self.assertLess(peak, total // 2)
        gen = results[-1]['generation']
        self.assertEqual(self.head(runtime), gen)
        self.assertIsNotNone(generation_digest(gen))
        self.assertEqual(len(self.members(runtime, gen)), 261)
        binding = runtime.binding_store.get(SCOPE, gen, self.incarnation)
        self.assertEqual(binding.catalog_scan_id,
                         runtime.capacity_status()['current']['scan_id'])
        answer = self.query(runtime, 'marker0257token')
        self.assertEqual((answer['status'], self.paths(answer)),
                         ('ok', ['plans/part12/doc17.md']))
        again = self.step(runtime)
        self.assertEqual((again['state'], again['generation']), ('unchanged', gen))


class SyncStepTests(CapacityProductCase):
    def test_add_modify_delete_publish_only_after_complete_scan(self):
        self.small_corpus()
        runtime = self.runtime()
        self.assertEqual(self.query(runtime, 'otter'),
                         {'status': 'pending', 'generation': None, 'rows': [],
                          'reason': 'no_published_generation'})
        first = self.drive(runtime)[-1]
        self.assertEqual(first['state'], 'indexed')
        gen1 = first['generation']
        self.assertEqual(self.marks, ['fenced'])
        self.assertEqual(self.members(runtime, gen1),
                         ['plans/a.md', 'plans/b.md', 'wiki/c.md'])

        self.write(self.plans, 'b.md', '# B\nbeta egret, rewritten longer\n')
        self.write(self.plans, 'd.md', '# D\ndelta crane\n')
        (self.wiki / 'c.md').unlink()
        # Before any rescan: changed/deleted candidates are pending, never served
        # stale; the new file is index lag (normal empty answer).
        heron = self.query(runtime, 'heron')
        self.assertEqual((heron['status'], heron['reason'], heron['rows']),
                         ('pending', 'candidates_unverified', []))
        self.assertEqual(heron['detail']['dropped'], {'hash_mismatch': 1})
        self.assertEqual(self.query(runtime, 'finch')['detail']['dropped'], {'missing': 1})
        self.assertEqual(self.query(runtime, 'crane'),
                         {'status': 'ok', 'generation': gen1, 'rows': []})
        self.assertEqual(self.paths(self.query(runtime, 'otter')), ['plans/a.md'])

        partial = self.step(runtime, rescan=True, max_files=1)
        self.assertEqual((partial['state'], partial['reason']), ('pending', 'scan_pending'))
        self.assertGreater(partial['catalog']['active']['backlog']['dirs_pending'], 0)
        # A partial scan deletes nothing: same head, same membership.
        self.assertEqual(self.head(runtime), gen1)
        self.assertEqual(self.members(runtime, gen1),
                         ['plans/a.md', 'plans/b.md', 'wiki/c.md'])
        self.assertEqual(partial['catalog']['current']['files'], 3)

        done = self.drive(runtime, max_files=1)
        self.assertEqual(done[-1]['state'], 'indexed')
        gen2 = done[-1]['generation']
        self.assertNotEqual(gen2, gen1)
        self.assertEqual(self.members(runtime, gen2),
                         ['plans/a.md', 'plans/b.md', 'plans/d.md'])
        self.assertEqual(self.members(runtime, gen1),  # immutable old generation
                         ['plans/a.md', 'plans/b.md', 'wiki/c.md'])
        self.assertEqual(self.paths(self.query(runtime, 'egret')), ['plans/b.md'])
        self.assertEqual(self.paths(self.query(runtime, 'crane')), ['plans/d.md'])
        self.assertEqual(self.query(runtime, 'finch')['rows'], [])
        self.assertEqual(self.query(runtime, 'heron')['status'], 'ok')
        self.assertEqual(self.step(runtime)['state'], 'unchanged')

    def test_source_changes_during_scan_and_before_stage_fail_closed(self):
        for i in range(6):
            self.write(self.plans, f'a{i:02d}.md', f'# A{i}\ndoc {i} otter\n')
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']

        # 1. A directory changes after it was listed: the scan blocks, the
        # step restarts it, and nothing is published from the partial scan.
        self.assertEqual(self.step(runtime, rescan=True, max_files=1)['reason'],
                         'scan_pending')
        self.write(self.plans, 'late.md', '# Late\nlate kestrel\n')
        results = self.drive(runtime, max_stats=1)
        restarted = [r for r in results if r.get('reason') == 'scan_restarted']
        self.assertEqual(len(restarted), 1)
        self.assertIn('plans/', restarted[0]['detail'])
        self.assertEqual(results[-1]['state'], 'indexed')
        self.assertEqual(self.published(runtime), 2)
        gen2 = results[-1]['generation']
        self.assertIn('plans/late.md', self.members(runtime, gen2))

        # 2. A member rewritten (same size) after its snapshot completed: the
        # streamed stage rehash refuses it, the claim is retired, nothing is
        # published, and a rescan picks the new bytes up.
        self.write(self.plans, 'a00.md', '# A0\ndoc 0 swift\n')
        runtime.request_scan()
        while runtime.catalog.step()['status'] == 'pending':
            pass
        self.write(self.plans, 'a00.md', '# A0\ndoc 0 crake\n')
        failed = self.step(runtime)
        self.assertEqual((failed['state'], failed['reason']), ('pending', 'replan'))
        self.assertEqual(self.head(runtime), gen2)
        self.assertEqual(runtime.queue.get(failed['claim'])['state'], 'replan')
        self.assertIn(failed['catalog']['active']['phase'],
                      laya_source_catalog.PENDING_PHASES)
        done = self.drive(runtime)[-1]
        self.assertEqual(done['state'], 'indexed')
        self.assertEqual(self.paths(self.query(runtime, 'crake')), ['plans/a00.md'])
        self.assertEqual(self.query(runtime, 'swift')['rows'], [])
        self.assertNotEqual(done['generation'], gen1)

    def test_root_branch_transition_rebinds_new_root(self):
        self.write(self.plans, 'a.md', '# A\nmain branch otter\n')
        self.write(self.wiki, 'w.md', '# W\nwiki page\n')
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        self.assertEqual(self.paths(self.query(runtime, 'otter')), ['plans/a.md'])

        # A branch switch that recreates the root directory: byte-identical
        # paths do not matter, the old binding's root node is gone.
        self.plans.rename(self.plans.with_name('plans-main'))
        self.plans.mkdir()
        self.write(self.plans, 'a.md', '# A\nfeature branch kestrel\n')
        self.write(self.plans, 'new.md', '# New\nfeature only plover\n')
        for term in ('otter', 'kestrel'):
            stale = self.query(runtime, term)
            self.assertEqual((stale['status'], stale['generation'], stale['rows']),
                             ('stale', gen1, []))
            self.assertEqual(stale['reason'], 'collection root replaced: plans')

        first = self.step(runtime, max_files=1)
        self.assertEqual((first['state'], first['reason']), ('pending', 'scan_pending'))
        self.assertEqual(first['snapshot_unusable'], 'collection root replaced: plans')
        self.assertEqual(self.head(runtime), gen1)
        done = self.drive(runtime)[-1]
        self.assertEqual(done['state'], 'indexed')
        gen2 = done['generation']
        binding = runtime.binding_store.get(SCOPE, gen2, self.incarnation)
        self.assertEqual(binding.roots[0][3], self.plans.stat().st_ino)
        self.assertEqual(self.paths(self.query(runtime, 'kestrel')), ['plans/a.md'])
        self.assertEqual(self.paths(self.query(runtime, 'plover')), ['plans/new.md'])
        self.assertEqual(self.query(runtime, 'otter')['rows'], [])
        self.assertEqual(self.step(runtime)['state'], 'unchanged')

    def test_pending_and_stale_statuses_and_recovery(self):
        self.small_corpus()
        runtime = self.runtime()
        # Legacy full sync still works on a capacity runtime, but its unbound
        # head is never served by the bound query.
        legacy = sync_full(runtime, before_publish=lambda: None, require_vector=False)
        self.assertEqual(legacy['state'], 'indexed')
        self.assertIsNone(generation_digest(legacy['generation']))
        self.assertEqual([r['path'] for r in runtime.query('"otter"', 5)], ['plans/a.md'])
        stale = self.query(runtime, 'otter')
        self.assertEqual((stale['status'], stale['reason']),
                         ('stale', 'generation name is not bound'))
        bound = self.drive(runtime)[-1]
        self.assertEqual(bound['state'], 'indexed')
        self.assertEqual(self.paths(self.query(runtime, 'otter')), ['plans/a.md'])

        runtime.index.exclude(SCOPE, path='plans/a.md')
        self.assertEqual(self.query(runtime, 'otter'),
                         {'status': 'stale', 'generation': None, 'rows': [],
                          'reason': 'exclusion_epoch_changed'})
        republished = self.step(runtime)
        self.assertEqual(republished['state'], 'indexed')
        self.assertEqual(self.query(runtime, 'otter')['rows'], [])
        self.assertEqual(self.paths(self.query(runtime, 'heron')), ['plans/b.md'])
        runtime.close()

        # A store without the head's binding: fail closed, then a new bound
        # generation is published instead of rebuilding the old binding.
        other = str(self.state / 'bindings-2.sqlite')
        self.new_store(other)
        fresh = self.runtime(binding_store_path=other)
        missing = self.query(fresh, 'heron')
        self.assertEqual((missing['status'], missing['reason']),
                         ('stale', 'generation source binding missing'))
        recovered = self.step(fresh)
        self.assertEqual(recovered['state'], 'indexed')
        self.assertNotEqual(recovered['generation'], republished['generation'])
        self.assertEqual(self.paths(self.query(fresh, 'heron')), ['plans/b.md'])

    def test_large_single_file_surfaces_required_budget(self):
        big = 6_000_000
        self.write(self.plans, 'big.md', sized('# Big\nbigterm\n', big))
        self.write(self.plans, 'small.md', '# Small\nsmall wren\n')
        runtime = self.runtime()
        results = self.drive(runtime)
        deferred = results[-1]
        self.assertEqual(deferred['state'], 'deferred')
        self.assertEqual(deferred['reason'], 'file_exceeds_step_bytes')
        self.assertEqual(deferred['required'], {'max_bytes': big})
        self.assertEqual(deferred['path'], 'plans/big.md')
        for _ in range(3):  # same budget: same answer, no byte read, no loop
            again = self.step(runtime)
            self.assertEqual((again['state'], again['required']), ('deferred', {'max_bytes': big}))
            self.assertEqual((again['work']['bytes_read'], again['work']['files_hashed']), (0, 0))
        self.assertIsNone(self.head(runtime))
        self.assertEqual(self.query(runtime, 'wren')['reason'], 'no_published_generation')
        done = self.drive(runtime, max_bytes=big)[-1]
        self.assertEqual(done['state'], 'indexed')
        over = self.query(runtime, 'bigterm')
        self.assertEqual((over['status'], over['detail']['dropped']),
                         ('pending', {'file_budget': 1}))
        served = self.query(runtime, 'bigterm', max_file_bytes=big, max_query_bytes=big)
        self.assertEqual(self.paths(served), ['plans/big.md'])

    def test_claim_backlog_retires_legacy_claims_and_owner_busy(self):
        self.small_corpus()
        runtime = self.runtime()
        for i in range(10):
            runtime.queue.enqueue_next(SCOPE, f'g-legacy{i}', 'a' * 64, None)
        with _owner_lock(Path(self.queue_path)) as owned:
            self.assertTrue(owned)
            self.assertEqual(self.step(runtime),
                             {'state': 'deferred', 'generation': None, 'claim': None,
                              'reason': 'owner_busy'})
        first = self.step(runtime, max_claims=4)
        self.assertEqual((first['state'], first['reason'], first['claims_pending']),
                         ('pending', 'claim_backlog', 6))
        second = self.step(runtime, max_claims=4)
        self.assertEqual(second['claims_pending'], 2)
        done = self.drive(runtime, max_claims=4)[-1]
        self.assertEqual(done['state'], 'indexed')
        states = {job['gen']: job['state'] for job in map(runtime.queue.get, [
            row[0] for row in runtime.queue.db.execute('SELECT claim FROM jobs')])}
        self.assertEqual({states[f'g-legacy{i}'] for i in range(10)}, {'replan'})
        self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])


class BindingGateTests(CapacityProductCase):
    def scanned(self, runtime):
        runtime.request_scan()
        while runtime.catalog.step()['status'] == 'pending':
            pass
        return runtime.catalog.require_complete()

    def test_persist_before_publish_and_mismatch_fail_closed(self):
        self.small_corpus()
        runtime = self.runtime()
        index, catalog, store = runtime.index, runtime.catalog, runtime.binding_store
        snapshot = self.scanned(runtime)
        binding = capture_bound_generation_binding(index, catalog, snapshot)
        job = enqueue_bound_generation(runtime.queue, binding, None)
        seal = {'base': None, 'claim_digest': job['claim_digest'],
                'claim_epoch': job['claim_epoch']}
        forged = dataclasses.replace(binding, source_manifest='0' * 64)
        with self.assertRaisesRegex(IndexContractError, 'generation_name_mismatch'):
            stage_bound_streaming(index, catalog, forged, **seal)
        self.assertEqual(stage_bound_streaming(index, catalog, binding, **seal), 'prepared')
        with self.assertRaisesRegex(IndexContractError, 'index_stale') as caught:
            publish_bound(index, catalog, store, binding, job)
        self.assertIn('not persisted before publish', str(caught.exception.__cause__))
        self.assertIsNone(self.head(runtime))
        self.assertIsNone(index.receipt(job['claim']))
        self.assertEqual(persist_generation_binding(store, index, binding)['status'], 'stored')
        other = dict(job, gen='g-' + 'f' * 32)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            publish_bound(index, catalog, store, binding, other)
        self.assertIsNone(self.head(runtime))
        self.assertEqual(publish_bound(index, catalog, store, binding, job), 'published')
        self.assertEqual(self.head(runtime), binding.generation)

        # A binding of a replaced snapshot never stages.
        old = capture_bound_generation_binding(index, catalog, catalog.require_complete())
        self.write(self.plans, 'a.md', '# A\nalpha otter, edited\n')
        self.scanned(runtime)
        with self.assertRaisesRegex(IndexContractError, 'index_stale'):
            stage_bound_streaming(index, catalog, old, base=binding.generation,
                                  claim_digest='d' * 64, claim_epoch=9)
        self.assertIsNone(index.db.execute('SELECT 1 FROM generations WHERE gen=?',
                                           (old.generation,)).fetchone())

    def test_existing_apis_remain_compatible(self):
        self.small_corpus()
        with self.assertRaisesRegex(IndexContractError, 'invalid_runtime_binding'):
            LayaIndexRuntime(**self.config(source_root=str(self.plans)))
        with self.assertRaisesRegex(IndexContractError, 'invalid_runtime_binding'):
            LayaIndexRuntime(**self.config(binding_store_path=None))
        with self.assertRaisesRegex(IndexContractError, 'invalid_runtime_binding'):
            LayaIndexRuntime(**self.config(binding_store_path=self.catalog_path))
        plain = self.runtime(catalog_path=None, binding_store_path=None)
        self.assertIsNone(plain.catalog)
        for call in (lambda: plain.query_bound('"otter"', 5), plain.request_scan,
                     lambda: self.step(plain)):
            with self.assertRaisesRegex(IndexContractError, 'capacity_not_configured'):
                call()
        self.assertEqual(sync_step(plain, before_publish=lambda: None, require_vector=True),
                         {'state': 'deferred', 'generation': None, 'claim': None,
                          'reason': 'vector_capacity_path_unavailable'})
        self.assertEqual(sync_full(plain, before_publish=lambda: None,
                                   require_vector=False)['state'], 'indexed')
        self.assertEqual([r['path'] for r in plain.query('"finch"', 5)], ['wiki/c.md'])
        plain.close()
        capacity = self.runtime()
        self.assertEqual(sync_full(capacity, before_publish=lambda: None,
                                   require_vector=False)['state'], 'unchanged')
        self.assertEqual([r['path'] for r in capacity.query('"otter"', 5)], ['plans/a.md'])
        self.assertEqual(capacity.inspect()['deletion_enabled'], False)


class ReviewFixTests(CapacityProductCase):
    """Independent capacity review: P2-1, P2-2 and P3."""

    def oversized_pending_scan(self, runtime):
        big = 6_000_000
        self.write(self.plans, 'big.md', sized('# Big\nbigterm\n', big))
        deferred = self.drive(runtime, rescan=True)[-1]
        self.assertEqual((deferred['state'], deferred['reason']),
                         ('deferred', 'file_exceeds_step_bytes'))
        self.assertIn(deferred['catalog']['active']['phase'],
                      laya_source_catalog.PENDING_PHASES)
        return big

    def test_exclusion_and_unbound_head_publish_while_scan_deferred(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        big = self.oversized_pending_scan(runtime)

        # P2-1: the exclusion is published from the COMPLETE snapshot while the
        # newer scan stays pending/deferred on the oversized file.
        runtime.index.exclude(SCOPE, path='plans/a.md')
        self.assertEqual(self.query(runtime, 'otter')['reason'], 'exclusion_epoch_changed')
        fixed = self.step(runtime)
        self.assertEqual(fixed['state'], 'indexed')
        gen2 = fixed['generation']
        self.assertNotEqual(gen2, gen1)
        self.assertIn(fixed['catalog']['active']['phase'], laya_source_catalog.PENDING_PHASES)
        self.assertEqual(fixed['catalog']['active']['deferred_reason'], 'file_exceeds_step_bytes')
        self.assertEqual(runtime.binding_store.get(SCOPE, gen2, self.incarnation).catalog_scan_id,
                         fixed['catalog']['current']['scan_id'])
        self.assertNotIn('plans/big.md', self.members(runtime, gen2))
        self.assertEqual(self.query(runtime, 'otter'),
                         {'status': 'ok', 'generation': gen2, 'rows': []})
        self.assertEqual(self.paths(self.query(runtime, 'heron')), ['plans/b.md'])
        for _ in range(2):  # served now: the scan answer again, no republish
            again = self.step(runtime)
            self.assertEqual((again['state'], again['reason'], again['generation']),
                             ('deferred', 'file_exceeds_step_bytes', gen2))
            self.assertEqual(again['work']['bytes_read'], 0)
        self.assertEqual(self.published(runtime), 2)
        runtime.close()

        # A head without a stored binding is replaced during the same deferral.
        other = str(self.state / 'bindings-2.sqlite')
        self.new_store(other)
        fresh = self.runtime(binding_store_path=other)
        self.assertEqual(self.query(fresh, 'heron')['reason'],
                         'generation source binding missing')
        rebound = self.step(fresh)
        self.assertEqual(rebound['state'], 'indexed')
        self.assertEqual(rebound['catalog']['active']['deferred_reason'],
                         'file_exceeds_step_bytes')
        self.assertEqual(self.paths(self.query(fresh, 'heron')), ['plans/b.md'])
        done = self.drive(fresh, max_bytes=big)[-1]
        self.assertEqual(done['state'], 'indexed')
        self.assertIn('plans/big.md', self.members(fresh, done['generation']))
        self.assertEqual(self.query(fresh, 'otter')['rows'], [])

    def test_unusable_snapshot_is_not_restaged_while_scan_pending(self):
        for i in range(4):
            self.write(self.plans, f'a{i:02d}.md', f'# A{i}\ndoc {i} otter\n')
        runtime = self.runtime()
        self.drive(runtime)
        big = self.oversized_pending_scan(runtime)
        # The head stops serving the snapshot (exclusion) and the snapshot can
        # no longer stage (a member changed after it completed).
        runtime.index.exclude(SCOPE, path='plans/a01.md')
        self.write(self.plans, 'a00.md', '# A0\ndoc 0 swift\n')
        staged = []
        real_stage = LayaIndexA.stage_stream

        def counted(self_, gen, *args, **kwargs):
            staged.append(gen)
            return real_stage(self_, gen, *args, **kwargs)

        with mock.patch.object(LayaIndexA, 'stage_stream', counted):
            failed = self.step(runtime)
            self.assertEqual((failed['state'], failed['reason'], failed['snapshot_unusable']),
                             ('pending', 'replan', 'bound_claim_replanned'))
            self.assertEqual(len(staged), 1)
            for _ in range(3):
                again = self.step(runtime)
                self.assertEqual((again['state'], again['reason'], again['snapshot_unusable']),
                                 ('deferred', 'file_exceeds_step_bytes',
                                  'bound_claim_replanned'))
                self.assertEqual(again['work']['bytes_read'], 0)
            self.assertEqual(len(staged), 1)  # no full re-read per call
            runtime.close()
            # The verdict is in-process: a new runtime pays one more attempt.
            second = self.runtime()
            self.assertEqual(self.step(second)['reason'], 'replan')
            self.assertEqual(self.step(second)['reason'], 'file_exceeds_step_bytes')
            self.assertEqual(len(staged), 2)
        self.assertEqual(self.published(second), 1)
        self.assertEqual(self.query(second, 'otter')['reason'], 'exclusion_epoch_changed')
        done = self.drive(second, max_bytes=big)[-1]
        self.assertEqual(done['state'], 'indexed')
        self.assertEqual(self.paths(self.query(second, 'swift')), ['plans/a00.md'])

    def test_query_bound_is_pending_while_a_stage_holds_the_write_lock(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        started, go, errors = threading.Event(), threading.Event(), []

        def stage():
            index = LayaIndexA(self.db_path)
            try:
                def documents():
                    started.set()  # BEGIN IMMEDIATE is held from here on
                    go.wait(30)
                    yield 'plans/a.md', 'alpha', True
                index.stage_stream('g-concurrent', SCOPE, documents(), base=gen1,
                                   source_manifest='0' * 64, claim_digest='d' * 64,
                                   claim_epoch=1)
            except IndexContractError as exc:
                errors.append(str(exc))
            finally:
                index.close()

        worker = threading.Thread(target=stage)
        worker.start()
        try:
            self.assertTrue(started.wait(30))
            busy = self.query(runtime, 'otter')
        finally:
            go.set()
            worker.join(30)
        self.assertEqual(busy, {'status': 'pending', 'generation': None, 'rows': [],
                                'reason': 'index_busy'})
        self.assertEqual(errors, ['manifest_mismatch'])
        self.assertEqual(self.pins(runtime), 0)
        self.assertEqual(self.paths(self.query(runtime, 'otter')), ['plans/a.md'])

    def test_busy_release_withholds_rows_and_is_retried(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        blocker = sqlite3.connect(self.db_path, isolation_level=None)
        self.addCleanup(blocker.close)
        real_search = cap.search_pinned_candidates

        def search_then_lock(*args, **kwargs):
            rows = real_search(*args, **kwargs)
            blocker.execute('BEGIN IMMEDIATE')  # a writer arrives before release
            return rows

        try:
            with mock.patch.object(cap, 'search_pinned_candidates', search_then_lock):
                held = self.query(runtime, 'otter')
        finally:
            if blocker.in_transaction:
                blocker.execute('ROLLBACK')
        self.assertEqual(held, {'status': 'pending', 'generation': gen1, 'rows': [],
                                'reason': 'index_busy', 'pin_released': False})
        self.assertEqual(self.pins(runtime), 1)
        served = self.query(runtime, 'heron')
        self.assertEqual(served, {'status': 'ok', 'generation': gen1,
                                  'rows': served['rows']})
        self.assertEqual(self.paths(served), ['plans/b.md'])
        self.assertEqual(self.pins(runtime), 0)  # the earlier pin was retried

    def test_corrupt_head_binding_fails_closed_and_is_replaced(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        self.corrupt_binding(gen1)
        stale = self.query(runtime, 'otter')
        self.assertEqual((stale['status'], stale['generation'], stale['rows'], stale['reason']),
                         ('stale', gen1, [], 'binding store corrupt: invalid binding record'))
        fixed = self.step(runtime)
        self.assertEqual(fixed['state'], 'indexed')
        self.assertNotEqual(fixed['generation'], gen1)
        self.assertEqual(fixed['binding_store'],
                         {'reason': 'binding_store_unavailable',
                          'detail': 'binding store corrupt: invalid binding record'})
        self.assertEqual(self.paths(self.query(runtime, 'otter')), ['plans/a.md'])
        again = self.step(runtime)
        self.assertEqual((again['state'], again['generation']),
                         ('unchanged', fixed['generation']))

    def test_busy_binding_store_still_steps_and_queries_fail_closed(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        blocker = sqlite3.connect(self.store_path, isolation_level=None)
        self.addCleanup(blocker.close)
        busy_store = {'reason': 'binding_store_busy', 'detail': 'binding store busy'}
        blocker.execute('BEGIN EXCLUSIVE')
        try:
            busy = self.query(runtime, 'otter')
            stepped = self.step(runtime, rescan=True, max_files=1)
        finally:
            blocker.execute('ROLLBACK')
        self.assertEqual(busy, {'status': 'pending', 'generation': gen1, 'rows': [],
                                'reason': 'binding_store_busy'})
        self.assertEqual((stepped['state'], stepped['reason']), ('pending', 'scan_pending'))
        self.assertEqual(stepped['binding_store'], busy_store)
        self.assertEqual(self.head(runtime), gen1)
        done = self.drive(runtime)[-1]
        self.assertEqual((done['state'], done['generation']), ('unchanged', gen1))

        # Idle catalog and a head that needs a republish: explicit deferred.
        runtime.index.exclude(SCOPE, path='plans/a.md')
        blocker.execute('BEGIN EXCLUSIVE')
        try:
            idle = self.step(runtime)
        finally:
            blocker.execute('ROLLBACK')
        self.assertEqual((idle['state'], idle['reason'], idle['generation'], idle['detail']),
                         ('deferred', 'binding_store_busy', gen1, 'binding store busy'))
        self.assertEqual(self.published(runtime), 1)
        self.assertEqual(self.step(runtime)['state'], 'indexed')
        self.assertEqual(self.query(runtime, 'otter')['rows'], [])

    def test_busy_store_at_publish_defers_and_same_claim_publishes(self):
        self.small_corpus()
        runtime = self.runtime()
        gen1 = self.drive(runtime)[-1]['generation']
        big = self.oversized_pending_scan(runtime)
        scan_id = runtime.catalog.status()['active']['scan_id']
        runtime.index.exclude(SCOPE, path='plans/a.md')
        store = runtime.binding_store
        blocker = sqlite3.connect(self.store_path, isolation_level=None)
        self.addCleanup(blocker.close)
        real_publish, real_get = cap.publish_bound, store.get
        held = []

        def publish(*args, **kwargs):
            held.append('publish')
            return real_publish(*args, **kwargs)

        def get(*args, **kwargs):
            if held != ['publish']:
                return real_get(*args, **kwargs)
            held.append('locked')  # only publish_bound's own store.get, once
            blocker.execute('BEGIN EXCLUSIVE')
            try:
                return real_get(*args, **kwargs)
            finally:
                blocker.execute('ROLLBACK')

        def forbidden(*_args, **_kwargs):
            raise AssertionError('a busy store must not request a new scan')

        with mock.patch.object(cap, 'publish_bound', publish), \
             mock.patch.object(store, 'get', get), \
             mock.patch.object(runtime.catalog, 'request_scan', forbidden):
            busy = self.step(runtime)
            self.assertEqual(held, ['publish', 'locked'])
            self.assertEqual((busy['state'], busy['reason']), ('deferred', 'deferred'))
            self.assertNotIn('snapshot_unusable', busy)
            self.assertIsNone(runtime._snapshot_verdict[1]
                              if runtime._snapshot_verdict else None)
            claim, gen2 = busy['claim'], busy['generation']
            self.assertNotEqual(runtime.queue.get(claim)['state'], 'replan')
            self.assertEqual([job['claim'] for job in runtime.queue.active_for_scope(SCOPE)],
                             [claim])
            self.assertEqual(self.head(runtime), gen1)
            self.assertIsNone(runtime.index.receipt(claim))
            # Fail closed while unpublished: the old head no longer serves.
            self.assertEqual(self.query(runtime, 'otter')['reason'], 'exclusion_epoch_changed')

            retry = self.step(runtime)
        self.assertEqual(held, ['publish', 'locked', 'publish'])
        # The retained claim publishes; the pending scan's deferral is the answer.
        self.assertEqual((retry['state'], retry['reason'], retry['generation']),
                         ('deferred', 'file_exceeds_step_bytes', gen2))
        self.assertNotIn('snapshot_unusable', retry)
        self.assertEqual(self.head(runtime), gen2)
        self.assertEqual(runtime.queue.get(claim)['state'], 'acked')
        self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
        self.assertEqual(self.published(runtime), 2)
        self.assertEqual(runtime.catalog.status()['active']['scan_id'], scan_id)
        self.assertEqual(self.query(runtime, 'otter'),
                         {'status': 'ok', 'generation': gen2, 'rows': []})
        self.assertEqual(self.paths(self.query(runtime, 'heron')), ['plans/b.md'])
        done = self.drive(runtime, max_bytes=big)[-1]
        self.assertEqual(done['state'], 'indexed')


class CrashReplayTests(CapacityProductCase):
    def crash(self, point):
        for i in range(5):
            self.write(self.plans, f'n{i}.md', f'# N{i}\nnote {i} ibis\n')
        child = subprocess.run(
            [sys.executable, '-B', '-c', CRASH_CHILD, str(CORE), point,
             json.dumps(self.config())],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(child.returncode, 70, child.stdout + child.stderr)
        return self.runtime()

    def active_job(self, runtime):
        jobs = runtime.queue.active_for_scope(SCOPE)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['state'], 'leased')
        return jobs[0]

    def finish(self, runtime):
        done = self.drive(runtime)[-1]
        self.assertEqual(done['state'], 'indexed')
        self.assertEqual(self.paths(self.query(runtime, 'ibis'))[:1], ['plans/n0.md'])
        runtime.index.inspect()
        return done

    def test_crash_after_persist_leaves_harmless_orphan(self):
        runtime = self.crash('after_persist')
        orphan = runtime.binding_store.verify()['generations']
        self.assertEqual(len(orphan), 1)
        self.assertEqual(runtime.queue.active_for_scope(SCOPE), [])
        self.assertIsNone(self.head(runtime))
        done = self.finish(runtime)
        self.assertNotIn(done['generation'], orphan)
        self.assertEqual(len(runtime.binding_store.verify()['generations']), 2)

    def test_crash_after_enqueue_replays_same_generation(self):
        runtime = self.crash('after_enqueue')
        job = self.active_job(runtime)
        self.assertIsNone(runtime.index.db.execute(
            'SELECT 1 FROM generations WHERE gen=?', (job['gen'],)).fetchone())
        self.assertEqual(self.finish(runtime)['generation'], job['gen'])

    def test_crash_mid_stage_rolls_back_and_replays(self):
        runtime = self.crash('mid_stage')
        job = self.active_job(runtime)
        self.assertIsNone(runtime.index.db.execute(
            'SELECT 1 FROM generations WHERE gen=?', (job['gen'],)).fetchone())
        self.assertEqual(self.finish(runtime)['generation'], job['gen'])

    def test_crash_after_stage_replay_rereads_every_member(self):
        runtime = self.crash('after_stage')
        job = self.active_job(runtime)
        self.assertEqual(runtime.index.db.execute(
            'SELECT state FROM generations WHERE gen=?', (job['gen'],)).fetchone()[0],
            'prepared')
        reads, staged = [], []
        real_read = laya_source_catalog._read_exact
        real_stage = LayaIndexA.stage_stream

        def counted(*args):
            reads.append(args[2])
            return real_read(*args)

        def recorded(self_, *args, **kwargs):
            staged.append(real_stage(self_, *args, **kwargs))
            return staged[-1]

        with mock.patch.object(laya_source_catalog, '_read_exact', counted), \
             mock.patch.object(LayaIndexA, 'stage_stream', recorded):
            self.assertEqual(self.finish(runtime)['generation'], job['gen'])
        self.assertEqual(staged, ['already_staged'])
        self.assertEqual(sorted(reads), [f'plans/n{i}.md' for i in range(5)])

    def test_unreadable_claim_binding_retires_claim(self):
        runtime = self.crash('after_enqueue')
        job = self.active_job(runtime)
        self.corrupt_binding(job['gen'])
        done = self.finish(runtime)
        self.assertNotEqual(done['generation'], job['gen'])
        self.assertEqual(runtime.queue.get(job['claim'])['state'], 'replan')

    def test_crash_after_publish_acks_without_restage(self):
        runtime = self.crash('after_publish')
        job = self.active_job(runtime)
        self.assertEqual(self.head(runtime), job['gen'])

        def forbidden(*_args, **_kwargs):
            raise AssertionError('receipt replay must not restage')

        with mock.patch.object(cap, 'stage_bound_streaming', forbidden):
            done = self.step(runtime)
        self.assertEqual((done['state'], done['generation'], done['claim']),
                         ('indexed', job['gen'], job['claim']))
        self.assertEqual(runtime.queue.get(job['claim'])['state'], 'acked')
        self.assertEqual(self.published(runtime), 1)


if __name__ == '__main__':
    unittest.main()
