"""Host recall over the capacity path: returned candidates only, no full scan.

Synthetic temp-dir corpora, a temp A-int DB, claim queue, catalog and binding
store only. The fill provider and the query provider are fakes; no model,
QMD, socket or host is started.
"""
from __future__ import annotations

import contextlib
import itertools
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import laya_host_response as response
import laya_index_source_port as source_port
from laya_index_a import IndexContractError, LayaIndexA
from laya_index_capacity import GenerationBindingStore, open_pinned_candidate_query
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import sync_step
import laya_source_catalog
from laya_source_catalog import CatalogPending, CorpusDiverged, LayaSourceCatalog
import laya_vector_capacity as vc

SCOPE = 'd' * 32


class FillProvider:
    profile = {'model_id': 'synthetic-precomputed-only',
               'embedding_fingerprint': 'f' * 64, 'dimension': 2}
    manifest = {'recipeId': vc.CANDIDATE_RECIPE}

    def embed_document_chunks(self, body, path):
        return [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}]


class HostCapacityCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='laya-host-capacity-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.raw = base / 'src' / 'raw'
        self.raw.mkdir(parents=True)
        self.roots = {'raw': str(self.raw)}
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
        fill = FillProvider()
        self.profile_id = vc.provider_profile_id(fill)
        self.route = {'profileId': self.profile_id, 'modelId': fill.profile['model_id'],
                      'embeddingFingerprint': fill.profile['embedding_fingerprint'],
                      'dimension': 2, 'metric': 'cosine',
                      'approvedProviderId': 'synthetic-provider',
                      'providerArtifactSha256': 'b' * 64,
                      'providerRelativePath': 'core/synthetic_provider.py'}
        (self.raw / 'a.md').write_text('# A\nalpha otter\n', encoding='utf-8')
        (self.raw / 'b.md').write_text('# B\nbeta heron\n', encoding='utf-8')
        self.runtime = LayaIndexRuntime(source_root=self.roots, scope=SCOPE,
                                        db_incarnation=self.incarnation, **self.paths)
        self.addCleanup(self.runtime.close)
        for _ in range(1000):
            result = sync_step(self.runtime, before_publish=lambda: None,
                               require_vector=True, vector_profile_id=self.profile_id)
            if result['state'] == 'pending' and result['reason'] == 'vector_backlog':
                while vc.vector_fill_step(self.runtime, fill)['state'] == 'pending':
                    pass
            elif result['state'] != 'pending':
                break
        self.assertEqual(result['state'], 'indexed')

    def provider(self, _prompt, route):
        return {'profileId': route['profileId'],
                'approvedProviderId': route['approvedProviderId'],
                'providerArtifactSha256': route['providerArtifactSha256'],
                'vector': [1.0, 0.0]}

    def phase(self, prompt, candidates):
        return response._v2_phase(self.runtime, self.token, prompt, self.route, self.provider,
                                  self.roots, ('raw',), (), 0, (), candidates)

    def pinned(self):
        self.token, _gen = self.runtime.index.pin(SCOPE)
        self.addCleanup(self.runtime.index.release, self.token)
        query = open_pinned_candidate_query(self.runtime.index, self.token,
                                            self.runtime.binding_store)
        self.addCleanup(query.close)
        return query

    def lexical(self, query):
        # Lexical only: 'otter' returns raw/a.md and never raw/b.md.
        return query.verify_rows(self.runtime.index.search(self.token, '"otter"', 5),
                                 collection_names=('raw',))

    def assertDiverged(self, query):
        with self.assertRaises(IndexContractError) as caught:
            query.prove_fresh()
        self.assertIs(type(caught.exception.__cause__), CorpusDiverged)
        self.assertEqual(response._stale_sync_code(caught.exception),
                         'index_stale_sync_queued')
        # A failed proof poisons the query; finish never makes rows deliverable.
        with self.assertRaises(IndexContractError):
            query.finish()

    def selected(self):
        """query_selected_v2 over the real capacity runtime, raw backfill only."""
        real_phase = response._v2_phase

        def raw_phase(runtime, token, prompt, profile, provider, _roots, _names, *rest):
            return real_phase(runtime, token, prompt, profile, provider, self.roots,
                              ('raw',), *rest)
        decision = {'projectRoot': str(self.raw.parent),
                    'collectionRoots': {'raw': str(self.raw), 'wiki': str(self.raw)},
                    'collections': [{'name': 'raw', 'role': 'raw'},
                                    {'name': 'wiki', 'role': 'wiki'}],
                    'recallSettings': {'recallStrategy': 'hierarchical',
                                       'lexicalPatterns': [], 'minScore': 0,
                                       'rawFallbackMinScore': 0, 'skipPaths': []}}
        import wiki_freshness
        with mock.patch.object(response, '_v2_phase', side_effect=raw_phase), \
                mock.patch.object(response, '_verified_wiki_row', return_value=False), \
                mock.patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                                  return_value=[]):
            return response.query_selected_v2(self.runtime, 'alpha otter', self.route,
                                              self.route, self.provider, decision)


class HostCapacityTests(HostCapacityCase):
    def test_candidates_are_verified_without_a_full_source_scan(self):
        query = self.pinned()
        with mock.patch.object(source_port, 'scan_collections',
                               side_effect=AssertionError('full scan on capacity path')):
            rows, evidence = self.phase('alpha otter', query)
        paths = [row['path'] for row in rows]
        self.assertEqual(paths[0], 'raw/a.md')
        self.assertTrue(evidence)
        self.assertIn('alpha otter', rows[0]['body'])
        stats = query.finish()
        self.assertEqual(stats['dropped'], {})
        # Only returned candidates were read, never a directory listing.
        self.assertLessEqual(stats['files_opened'], 2)

    def test_changed_file_is_dropped_not_served_and_query_survives(self):
        query = self.pinned()
        (self.raw / 'a.md').write_text('# A\nalpha otter, edited after sync\n',
                                       encoding='utf-8')
        rows, _evidence = self.phase('alpha otter', query)
        self.assertNotIn('raw/a.md', [row['path'] for row in rows])
        self.assertTrue(all('edited' not in row['body'] for row in rows))
        self.assertGreaterEqual(query.finish()['dropped'].get('hash_mismatch', 0), 1)

    def test_foreign_generation_row_poisons_the_whole_query(self):
        query = self.pinned()
        good = query.verify_rows(self.runtime.index.search(self.token, '"otter"', 5),
                                 collection_names=('raw',))
        self.assertTrue(good)
        foreign = dict(good[0], generation='g-' + '0' * 32)
        with self.assertRaises(IndexContractError):
            query.verify_rows([foreign], collection_names=('raw',))
        with self.assertRaises(IndexContractError):
            query.finish()

    def test_full_scan_path_still_fails_closed_on_the_same_change(self):
        self.pinned()
        (self.raw / 'a.md').write_text('# A\nalpha otter, edited after sync\n',
                                       encoding='utf-8')
        with self.assertRaises(IndexContractError):
            self.phase('alpha otter', None)

    def test_rows_are_withheld_when_finish_fails(self):
        query = self.pinned()
        self.phase('alpha otter', query)
        with mock.patch.object(type(query), 'finish',
                               side_effect=IndexContractError('index_stale')):
            runtime = SimpleNamespace(index=self.runtime.index, scope=SCOPE,
                                      binding_store=self.runtime.binding_store)
            decision = {'projectRoot': str(self.raw.parent),
                        'collectionRoots': {'raw': str(self.raw), 'wiki': str(self.raw)},
                        'collections': [{'name': 'raw', 'role': 'raw'},
                                        {'name': 'wiki', 'role': 'wiki'}],
                        'recallSettings': {'recallStrategy': 'hierarchical',
                                           'lexicalPatterns': [], 'minScore': 0,
                                           'rawFallbackMinScore': 0, 'skipPaths': []}}
            import wiki_freshness
            with mock.patch.object(response, '_v2_phase',
                                   return_value=([{'path': 'raw/a.md'}], True)), \
                    mock.patch.object(response, '_verified_wiki_row', return_value=True), \
                    mock.patch.object(wiki_freshness, 'unresolved_pending_refreshes_strict',
                                      return_value=[]):
                with self.assertRaisesRegex(IndexContractError, 'index_stale'):
                    response.query_selected_v2(runtime, 'alpha otter', self.route,
                                               self.route, self.provider, decision)

    def test_unbound_head_fails_closed_before_any_search(self):
        runtime = SimpleNamespace(index=self.runtime.index, scope=SCOPE,
                                  binding_store=self.runtime.binding_store)
        decision = {'collectionRoots': {'raw': str(self.raw), 'wiki': str(self.raw)},
                    'collections': [{'name': 'raw', 'role': 'raw'},
                                    {'name': 'wiki', 'role': 'wiki'}],
                    'recallSettings': {'recallStrategy': 'hierarchical',
                                       'lexicalPatterns': [], 'minScore': 0,
                                       'rawFallbackMinScore': 0, 'skipPaths': []}}
        import laya_index_capacity
        with mock.patch.object(laya_index_capacity, 'load_pinned_binding',
                               side_effect=IndexContractError('index_stale')), \
                mock.patch.object(response, '_v2_phase',
                                  side_effect=AssertionError('searched without a binding')):
            with self.assertRaises(IndexContractError):
                response.query_selected_v2(runtime, 'alpha otter', self.route, self.route,
                                           self.provider, decision)

    def test_unchanged_corpus_is_proven_before_delivery(self):
        query = self.pinned()
        self.assertEqual([row['path'] for row in self.lexical(query)], ['raw/a.md'])
        proof = query.prove_fresh()
        self.assertEqual((proof['files_known'], proof['files_hashed']), (2, 2))
        self.assertTrue(query.finish()['freshness']['proven'])

    def test_changed_returned_file_blocks_instead_of_an_empty_answer(self):
        query = self.pinned()
        (self.raw / 'a.md').write_text('# A\nalpha otter, edited after sync\n',
                                       encoding='utf-8')
        # The returned-row check alone yields an empty, old-generation answer.
        self.assertEqual(self.lexical(query), [])
        self.assertDiverged(query)

    def test_unreturned_new_renamed_or_changed_file_blocks(self):
        b_text = (self.raw / 'b.md').read_text(encoding='utf-8')
        cases = {
            'created': (lambda: (self.raw / 'c.md').write_text('# C\ngamma lynx\n',
                                                               encoding='utf-8'),
                        lambda: (self.raw / 'c.md').unlink()),
            'created_in_new_dir': (
                lambda: ((self.raw / 'new').mkdir(),
                         (self.raw / 'new' / 'd.md').write_text('# D\n', encoding='utf-8')),
                lambda: ((self.raw / 'new' / 'd.md').unlink(), (self.raw / 'new').rmdir())),
            'renamed': (lambda: (self.raw / 'b.md').rename(self.raw / 'b2.md'),
                        lambda: (self.raw / 'b2.md').rename(self.raw / 'b.md')),
            # Same size: only the bytes differ.
            'bytes_changed': (lambda: (self.raw / 'b.md').write_text(
                                  b_text.replace('heron', 'HERON'), encoding='utf-8'),
                              lambda: (self.raw / 'b.md').write_text(b_text,
                                                                     encoding='utf-8')),
        }
        for name, (mutate, restore) in cases.items():
            with self.subTest(name):
                query = self.pinned()
                mutate()
                try:
                    # raw/a.md is returned, verified and untouched.
                    self.assertEqual([row['path'] for row in self.lexical(query)],
                                     ['raw/a.md'])
                    self.assertEqual(query.stats['dropped'], {})
                    self.assertDiverged(query)
                finally:
                    restore()
                # Restored bytes prove fresh again although stat data moved.
                query = self.pinned()
                self.lexical(query)
                query.prove_fresh()
                query.finish()

    def test_over_budget_or_late_proof_is_unproven_before_reading_bytes(self):
        for name, kwargs, patch in (
                ('files', {'max_files': 1}, None),
                ('bytes', {'max_bytes': 10}, None),
                ('deadline', {}, itertools.count(0.0, 10.0))):
            with self.subTest(name):
                query = self.pinned()
                self.lexical(query)
                clock = (mock.patch.object(laya_source_catalog.time, 'monotonic',
                                           side_effect=patch)
                         if patch else contextlib.nullcontext())
                with clock:
                    with self.assertRaises(CatalogPending) as caught:
                        query.prove_fresh(**kwargs)
                detail = caught.exception.detail
                self.assertEqual(detail['reason'], 'freshness_unproven')
                self.assertEqual(detail['bytes_read'], 0)
                self.assertEqual(response._stale_sync_code(caught.exception),
                                 'index_freshness_unproven_sync_queued')
                with self.assertRaises(IndexContractError):
                    query.finish()

    def test_selected_recall_withholds_rows_for_an_unreturned_new_file(self):
        rows, _title_only = self.selected()
        self.assertIn('raw/a.md', [row['path'] for row in rows])
        (self.raw / 'c.md').write_text('# C\ngamma lynx\n', encoding='utf-8')
        with self.assertRaises(IndexContractError) as caught:
            self.selected()
        self.assertIs(type(caught.exception.__cause__), CorpusDiverged)


class ServeRuntimeTests(unittest.TestCase):
    """serve_selected opens the runtime the capacity path needs, or the old one."""

    def serve(self, with_stores):
        tmp = tempfile.TemporaryDirectory(prefix='laya-host-serve-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        db = base / 'index.sqlite'
        if with_stores:
            for suffix in ('.catalog.sqlite', '.bindings.sqlite'):
                path = base / ('index.sqlite' + suffix)
                path.write_text('')
                path.chmod(0o600)
        roots = {'raw': str(base / 'raw'), 'wiki': str(base / 'wiki')}
        settings = {'topN': 3, 'lexicalPatterns': []}
        decision = {'pluginRoot': str(CORE.parent), 'projectRoot': str(base),
                    'scopeId': SCOPE, 'dbPath': str(db), 'queuePath': str(base / 'q'),
                    'dbIncarnation': 'e' * 32, 'collectionRoots': roots,
                    'collections': [], 'recallSettings': settings,
                    'vectorProfile': {}}
        proof = {'productRouteAllowed': True, 'providerManifest': {}, 'vectorProfile': {}}
        opened = {}

        def runtime(**kwargs):
            opened.update(kwargs)
            context = mock.MagicMock()
            context.__enter__.return_value = context
            return context
        import config
        import laya_index_runtime
        import laya_install_journal
        import laya_local_embed_provider
        with mock.patch.object(laya_install_journal, 'verify_selected_route',
                               return_value=proof), \
                mock.patch.object(laya_local_embed_provider, 'ReviewedLocalQmdProvider'), \
                mock.patch.object(config, 'load_project_config', return_value=settings), \
                mock.patch.object(laya_index_runtime, 'LayaIndexRuntime', side_effect=runtime), \
                mock.patch.object(response, 'query_selected_v2',
                                  return_value=([], frozenset())), \
                mock.patch.object(response, 'render_context', return_value=''), \
                mock.patch.object(response, 'host_payload', return_value={}):
            response.serve_selected('recall', 'claude', decision, {'prompt': 'alpha'})
        return opened, roots, str(db)

    def test_stores_open_all_roots_and_both_stores(self):
        opened, roots, db = self.serve(True)
        self.assertEqual(opened['source_root'], roots)
        self.assertEqual((opened['catalog_path'], opened['binding_store_path']),
                         (db + '.catalog.sqlite', db + '.bindings.sqlite'))

    def test_without_stores_the_runtime_is_unchanged(self):
        opened, roots, _db = self.serve(False)
        self.assertEqual(opened['source_root'], next(iter(roots.values())))
        self.assertNotIn('catalog_path', opened)

    def test_stale_or_unproven_capacity_blocks_and_requests_refresh(self):
        diverged = IndexContractError('index_stale')
        diverged.__cause__ = CorpusDiverged('live source differs')
        cases = (
            ('diverged', diverged, 'index_stale_sync_queued'),
            ('unproven', CatalogPending({'reason': 'freshness_unproven'}),
             'index_freshness_unproven_sync_queued'),
            # Not a freshness verdict: unchanged failure, no wake.
            ('other', CatalogPending({'reason': 'candidates_unverified'}), None))
        for name, failure, code in cases:
            with self.subTest(name):
                tmp = tempfile.TemporaryDirectory(prefix='laya-host-serve-')
                self.addCleanup(tmp.cleanup)
                base = Path(tmp.name).resolve()
                settings = {'topN': 3, 'lexicalPatterns': []}
                decision = {'pluginRoot': str(CORE.parent), 'projectRoot': str(base),
                            'scopeId': SCOPE, 'dbPath': str(base / 'index.sqlite'),
                            'queuePath': str(base / 'q'), 'dbIncarnation': 'e' * 32,
                            'collectionRoots': {'raw': str(base / 'raw')},
                            'collections': [], 'recallSettings': settings,
                            'vectorProfile': {}}
                proof = {'productRouteAllowed': True, 'providerManifest': {},
                         'vectorProfile': {}, 'selectionEpoch': 0}
                context = mock.MagicMock()
                context.__enter__.return_value = context
                context.__exit__.return_value = False
                import config
                import laya_index_runtime
                import laya_install_journal
                import laya_local_embed_provider
                import runtime_work
                with mock.patch.object(laya_install_journal, 'verify_selected_route',
                                       return_value=proof), \
                        mock.patch.object(laya_local_embed_provider,
                                          'ReviewedLocalQmdProvider'), \
                        mock.patch.object(config, 'load_project_config',
                                          return_value=settings), \
                        mock.patch.object(laya_index_runtime, 'LayaIndexRuntime',
                                          return_value=context), \
                        mock.patch.object(response, 'query_selected_v2',
                                          side_effect=failure), \
                        mock.patch.object(response, 'render_context',
                                          side_effect=AssertionError('rendered')), \
                        mock.patch.object(runtime_work, 'request',
                                          return_value={'status': 'queued'}) as wake:
                    if code is None:
                        with self.assertRaises(CatalogPending):
                            response.serve_selected('recall', 'claude', decision,
                                                    {'prompt': 'alpha'})
                        wake.assert_not_called()
                    else:
                        with self.assertRaisesRegex(response.HostResponseError,
                                                    f'^{code}$'):
                            response.serve_selected('recall', 'claude', decision,
                                                    {'prompt': 'alpha'})
                        wake.assert_called_once_with('laya-sync', str(base))


if __name__ == '__main__':
    unittest.main()
