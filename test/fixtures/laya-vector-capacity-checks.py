"""Disposable vector-fill checks for the capacity path (no model, no network).

Synthetic temp-dir corpora, a temp A-int DB, claim queue, catalog and binding
store only. The provider is a fake that returns one fixed 2-D chunk per file
and counts calls.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_capacity import GenerationBindingStore
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import sync_step
from laya_index_vector import LayaVectorIndex, verify_generation_seal
from laya_source_catalog import LayaSourceCatalog
import laya_vector_capacity as vc

SCOPE = 'c' * 32


class FakeProvider:
    profile = {'model_id': 'synthetic-precomputed-only',
               'embedding_fingerprint': 'f' * 64, 'dimension': 2}
    manifest = {'recipeId': vc.CANDIDATE_RECIPE}

    def __init__(self):
        self.calls = []

    def embed_document_chunks(self, body, path):
        self.calls.append(path)
        return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}]


class VectorFillCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-vector-capacity-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.plans = base / 'src' / 'plans'
        self.plans.mkdir(parents=True)
        self.roots = {'plans': str(self.plans)}
        state = base / 'state'
        state.mkdir()
        self.paths = {'db_path': str(state / 'index.sqlite'),
                      'queue_path': str(state / 'queue.sqlite'),
                      'catalog_path': str(state / 'catalog.sqlite'),
                      'binding_store_path': str(state / 'bindings.sqlite')}
        index = LayaIndexA(self.paths['db_path'], create=True)
        self.incarnation = index.incarnation()
        index.close()
        ClaimQueue.create(self.paths['queue_path'],
                          index_incarnation=self.incarnation).close()
        LayaSourceCatalog(self.paths['catalog_path'], self.roots, SCOPE, create=True).close()
        GenerationBindingStore(self.paths['binding_store_path'], scope=SCOPE,
                               db_incarnation=self.incarnation, roots=self.roots,
                               create=True).close()
        self.provider = FakeProvider()
        self.profile_id = vc.provider_profile_id(self.provider)
        self.fenced = []

    def runtime(self):
        runtime = LayaIndexRuntime(source_root=self.roots, scope=SCOPE,
                                   db_incarnation=self.incarnation, **self.paths)
        self.addCleanup(runtime.close)
        return runtime

    def write(self, rel, text):
        path = self.plans / rel
        path.write_text(text, encoding='utf-8')
        return path

    def corpus(self, n):
        for i in range(n):
            self.write(f'f{i}.md', f'# F{i}\nbody {i}\n')

    def step(self, runtime, **kw):
        return sync_step(runtime, before_publish=lambda: self.fenced.append(1),
                         require_vector=True, vector_profile_id=self.profile_id, **kw)

    def scan_to_complete(self, runtime):
        """Drive sync_step until it stops scanning (a snapshot exists)."""
        for _ in range(1000):
            result = self.step(runtime)
            if result['reason'] != 'scan_pending':
                return result
        raise AssertionError('scan did not complete')

    def fill(self, runtime, **kw):
        answers = []
        for _ in range(1000):
            answers.append(vc.vector_fill_step(runtime, self.provider, **kw))
            # Only a budget stop is resumed here; anything else is the
            # caller's (a changed member needs sync_step to step the rescan).
            if answers[-1]['state'] != 'pending' or answers[-1]['reason'] != 'vector_backlog':
                return answers
        raise AssertionError('fill did not settle')

    def head(self, runtime):
        row = runtime.index.db.execute('SELECT gen FROM head WHERE scope=?',
                                       (SCOPE,)).fetchone()
        return row[0] if row else None

    def sync(self, runtime):
        """Worker-shaped loop: sync_step, filling whenever it asks for vectors."""
        for _ in range(1000):
            result = self.step(runtime)
            if result['state'] == 'pending' and result['reason'] == 'vector_backlog':
                self.fill(runtime, max_files=2)
                continue
            if result['state'] != 'pending':
                return result
        raise AssertionError('sync did not settle')


class VectorFillTests(VectorFillCase):
    def test_backlog_gates_capture_until_fill_completes(self):
        self.corpus(3)
        runtime = self.runtime()
        gated = self.scan_to_complete(runtime)
        self.assertEqual((gated['state'], gated['reason']), ('pending', 'vector_backlog'))
        self.assertIsNone(self.head(runtime))
        self.assertEqual(self.fenced, [])  # nothing captured, staged or fenced
        self.assertEqual(self.provider.calls, [])  # sync_step never embeds

    def test_fill_resumes_across_steps_and_embeds_each_file_once(self):
        self.corpus(5)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        answers = self.fill(runtime, max_files=2)
        self.assertEqual([a['state'] for a in answers],
                         ['pending', 'pending', 'complete'])
        self.assertEqual([a['embedded'] for a in answers], [2, 2, 1])
        self.assertEqual(sorted(self.provider.calls),
                         [f'plans/f{i}.md' for i in range(5)])
        again = vc.vector_fill_step(runtime, self.provider)
        self.assertEqual((again['state'], again['embedded']), ('complete', 0))
        # Content written ahead of stage, with no membership yet, is not damage.
        runtime.index.inspect()
        self.assertTrue(vc.fill_complete(runtime, self.profile_id))

    def test_progress_survives_a_new_runtime(self):
        self.corpus(4)
        first = self.runtime()
        self.scan_to_complete(first)
        self.assertEqual(vc.vector_fill_step(first, self.provider, max_files=3)['embedded'], 3)
        first.close()
        second = self.runtime()
        done = self.fill(second, max_files=3)
        self.assertEqual(done[-1]['state'], 'complete')
        self.assertEqual(len(self.provider.calls), 4)

    def test_complete_fill_publishes_a_sealed_generation(self):
        self.corpus(3)
        runtime = self.runtime()
        result = self.sync(runtime)
        self.assertEqual(result['state'], 'indexed')
        gen = result['generation']
        self.assertEqual(self.head(runtime), gen)
        runtime.index.db.execute('BEGIN')
        try:
            proof = verify_generation_seal(runtime.index, gen)
        finally:
            runtime.index.db.commit()
        self.assertEqual(proof['profile_id'], self.profile_id)
        self.assertEqual(self.step(runtime)['state'], 'unchanged')
        self.assertEqual(len(self.provider.calls), 3)

    def test_rescan_after_one_change_embeds_only_that_file(self):
        self.corpus(4)
        runtime = self.runtime()
        first = self.sync(runtime)['generation']
        self.provider.calls.clear()
        self.write('f2.md', '# F2\nbody two, rewritten\n')
        result = self.step(runtime, rescan=True)
        self.assertIn(result['state'], ('pending', 'indexed'))
        final = self.sync(runtime)
        self.assertEqual(final['state'], 'indexed')
        self.assertNotEqual(final['generation'], first)
        self.assertEqual(self.provider.calls, ['plans/f2.md'])

    def test_should_yield_stops_before_the_next_embed(self):
        self.corpus(3)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        calls = []

        def should_yield():
            calls.append(1)
            return len(calls) > 1
        answer = vc.vector_fill_step(runtime, self.provider, should_yield=should_yield)
        self.assertEqual((answer['state'], answer['reason'], answer['embedded']),
                         ('pending', 'vector_backlog', 1))
        self.assertEqual(self.provider.calls, ['plans/f0.md'])
        self.assertEqual(answer['cursor'], 'plans/f0.md')

    def test_lost_progress_record_replays_as_cached(self):
        self.corpus(2)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        with mock.patch.object(LayaSourceCatalog, 'set_vector_fill', return_value=False):
            busy = vc.vector_fill_step(runtime, self.provider)
        self.assertEqual((busy['state'], busy['reason'], busy['embedded']),
                         ('deferred', 'catalog_busy', 1))
        done = self.fill(runtime)
        self.assertEqual(done[-1]['state'], 'complete')
        self.assertEqual(sum(a['cached'] for a in done), 1)
        self.assertEqual(self.provider.calls, ['plans/f0.md', 'plans/f1.md'])

    def test_byte_budget_bounds_reads_but_always_makes_progress(self):
        self.write('big.md', '# Big\n' + 'x' * 5000 + '\n')
        self.write('small.md', '# Small\nsmall\n')
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        first = vc.vector_fill_step(runtime, self.provider, max_bytes=100)
        self.assertEqual((first['state'], first['embedded']), ('pending', 1))
        self.assertGreater(first['bytes'], 100)  # one oversized member is still read
        second = vc.vector_fill_step(runtime, self.provider, max_bytes=100)
        self.assertEqual(second['embedded'], 1)
        self.assertEqual(self.fill(runtime, max_bytes=100)[-1]['state'], 'complete')

    def test_changed_member_requests_rescan_and_embeds_nothing(self):
        self.corpus(2)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        self.write('f0.md', '# F0\nchanged after the scan\n')
        answer = vc.vector_fill_step(runtime, self.provider)
        self.assertEqual((answer['state'], answer['reason'], answer['path']),
                         ('pending', 'source_changed', 'plans/f0.md'))
        self.assertEqual(self.provider.calls, [])
        self.assertIn(runtime.catalog.status()['active']['phase'],
                      ('enumerate', 'hash', 'verify_dirs', 'verify_files'))
        final = self.sync(runtime)
        self.assertEqual(final['state'], 'indexed')
        self.assertEqual(sorted(self.provider.calls), ['plans/f0.md', 'plans/f1.md'])

    def test_missing_chunk_set_fails_closed_without_publishing(self):
        self.corpus(2)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        snapshot = runtime.catalog.require_complete()
        # Claim the fill is done without caching anything.
        self.assertTrue(runtime.catalog.set_vector_fill(snapshot, self.profile_id, '', True))
        result = self.step(runtime)
        self.assertIsNone(self.head(runtime))
        self.assertNotEqual(result['state'], 'indexed')
        self.assertEqual(self.provider.calls, [])
        published = runtime.index.db.execute(
            "SELECT COUNT(*) FROM generations WHERE state='published'").fetchone()[0]
        self.assertEqual(published, 0)

    def test_members_page_matches_manifest_rows(self):
        self.corpus(3)
        runtime = self.runtime()
        self.scan_to_complete(runtime)
        catalog = runtime.catalog
        snapshot = catalog.require_complete()
        from laya_source_catalog import load_exclusions
        exclusions = load_exclusions(runtime.index, SCOPE)
        page = catalog.members(snapshot, exclusions, limit=2)
        rest = catalog.members(snapshot, exclusions, after=page[-1][0])
        self.assertEqual([row[0] for row in page + rest],
                         ['plans/f0.md', 'plans/f1.md', 'plans/f2.md'])
        self.assertTrue(all(row[2] for row in page + rest))
        self.assertEqual([catalog.lookup(snapshot, row[0])[0] for row in page + rest],
                         [row[1] for row in page + rest])

    def test_require_vector_needs_a_profile(self):
        runtime = self.runtime()
        with self.assertRaises(TypeError):
            sync_step(runtime, before_publish=lambda: None, require_vector=True)

    def test_provider_without_candidate_recipe_is_refused(self):
        class Wrong(FakeProvider):
            manifest = {'recipeId': 'other'}
        with self.assertRaises(IndexContractError):
            vc.provider_profile_id(Wrong())


if __name__ == '__main__':
    unittest.main()
