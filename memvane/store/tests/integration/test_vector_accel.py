"""Pinned E-path reads, candidate validation and lexical preservation."""
import importlib
import math
import struct
import sys
from unittest import mock

from memvane.store.tests import support


class AcceleratedStoreChecks(support.VectorCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'on'})
        self.env.start()
        self.addCleanup(self.env.stop)
        if sys.platform != 'darwin':
            self.skipTest('native Accelerate requires macOS')

    def ready_many(self, count=80):
        store, _repo, h, manifest, r, p = self.configured(count)
        for i, member in enumerate(manifest['members']):
            self.put_vectors(store, member['blob_sha'], r, p, [[1., (i + 1) / count]])
        gen, pin = self.publish(store, h, manifest, r, p)
        self.addCleanup(store.release, pin)
        return store, h, manifest, r, p, gen, pin

    def test_empty_partial_scan_records_coverage_without_a_vector_source(self):
        store, _repo, handle, manifest, recipe, profile = self.configured(1, publish=False)
        manifest['members'] = []
        self.lexical(store, handle, manifest)
        pin = store.pin_view(handle)
        self.addCleanup(store.release, pin)
        for switch in ('on', 'off'):
            with self.subTest(switch=switch), mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': switch}):
                timings = {}
                rows = store.search_exact(pin, [1., 0.], 5, profile_id=profile,
                                          partial_recipe_id=recipe, timings=timings)
                self.assertEqual((rows, timings['vectorRows'], timings['vectorEligibleMembers'],
                                  timings['vectorCoveredMembers']), ([], 0, 0, 0))
                self.assertNotIn('vectorSource', timings)

    def test_same_top_twenty_exact_scores_with_only_sixty_validated_candidates(self):
        store, _h, _manifest, _r, p, _gen, pin = self.ready_many()
        with mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}):
            expected = store.search_exact(pin, [1., 0.], 20, profile_id=p)
        stats = {}
        with mock.patch.object(store, '_vectors', wraps=store._vectors) as validate:
            actual = store.search_exact(pin, [1., 0.], 20, profile_id=p,
                                        max_float_work=1, timings=stats)
        self.assertEqual(actual, expected)
        self.assertEqual(validate.call_count, 60)
        self.assertEqual((stats['vectorKernel'], stats['vectorSource'], stats['vectorRows'],
                          stats['vectorVerified']), ('accelerate', 'store', 80, 60))
        for field in ('vectorLoadMs', 'vectorScanMs', 'vectorVerifyMs',
                      'vectorReadMs', 'vectorUnpackMs', 'vectorDotMs'):
            self.assertTrue(math.isfinite(stats[field]) and stats[field] >= 0, field)

    def test_row_cap_rejects_before_vector_read_and_keeps_all_lexical_candidates(self):
        store, _h, _m, _r, p, _g, pin = self.ready_many(4)
        accel = importlib.import_module('memvane.store.view.view_accel_search')
        lexical = store.search(pin, 'hello', 10)
        queries = []
        store._db.set_trace_callback(queries.append)
        self.addCleanup(store._db.set_trace_callback, None)
        with mock.patch.object(accel, 'MAX_ROWS', 3):
            answer = store.search_hybrid(pin, 'hello', [1., 0.], 10, profile_id=p)
        # No arena covers these sets: the store read they need is over the bound until one does.
        self.assertEqual(answer['semantic_status'], 'deferred_cache_building')
        self.assertEqual(answer['lexical'], lexical)
        self.assertEqual(len(answer['lexical']), 3)  # The seed card has no "hello" token.
        self.assertEqual(answer['semantic'], [])
        self.assertFalse(any('FROM vectors ' in q for q in queries))
        with mock.patch.object(accel, 'MAX_ROWS', 3), \
                mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'store'}):
            switched = store.search_hybrid(pin, 'hello', [1., 0.], 10, profile_id=p)
        self.assertEqual(switched['semantic_status'], 'deferred_work_budget')
        self.assertEqual(switched['lexical'], lexical)
        with mock.patch.object(accel, 'MAX_ROWS', 4):
            self.assertEqual(len(store.search_exact(pin, [1., 0.], 10, profile_id=p)), 4)
        with mock.patch.object(accel, 'MAX_BYTES', 4 * 2 * 4 - 1):
            self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 10,
                                         profile_id=p).reason, 'vector_cache_building')

    def test_semantic_deadline_preserves_lexical_candidates(self):
        store, _h, _m, _r, p, _g, pin = self.ready_many(4)
        from memvane.store.view import vector_accel
        from memvane.store.blob.blob_values import BlobStoreError
        lexical = store.search(pin, 'hello', 10)
        native = vector_accel.kernel()
        self.assertIsNotNone(native)
        with mock.patch.object(native, 'scores', side_effect=BlobStoreError('vector_deadline_exceeded')):
            result = store.search_hybrid(pin, 'hello', [1., 0.], 10, profile_id=p)
        self.assertEqual(result['semantic_status'], 'deferred_deadline')
        self.assertEqual(result['lexical'], lexical)
        self.assertEqual(len(result['lexical']), 3)
        self.assertEqual(result['semantic'], [])

    def test_load_and_native_allocation_failures_preserve_lexical_candidates(self):
        store, _h, _m, _r, p, _g, pin = self.ready_many(4)
        from memvane.store.view import vector_accel, view_accel_search
        lexical = store.search(pin, 'hello', 10)
        native = vector_accel.kernel()
        for failure in (mock.patch.object(view_accel_search, '_load', side_effect=MemoryError),
                        mock.patch.object(native, 'scores', side_effect=MemoryError)):
            with failure:
                result = store.search_hybrid(pin, 'hello', [1., 0.], 10, profile_id=p)
            self.assertEqual(result['semantic_status'], 'deferred_work_budget')
            self.assertEqual(result['lexical'], lexical)
            self.assertEqual(len(result['lexical']), 3)
            self.assertEqual(result['semantic'], [])

    def test_unavailable_kernel_and_switch_retain_python_work_cap(self):
        store, _h, _m, _r, p, _g, pin = self.ready_many(4)
        from memvane.store.view import vector_accel
        for context in (mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}),
                        mock.patch.object(vector_accel, 'kernel', return_value=None)):
            with context:
                stats = {}
                reason = self.refused(store.search_exact, pin, [1., 0.], 10,
                                      profile_id=p, max_float_work=1, timings=stats).reason
                self.assertEqual(reason, 'vector_work_exceeded')
                self.assertEqual(stats['vectorKernel'], 'python')

    def test_candidate_digest_failure_and_non_candidate_invalid_values_refuse(self):
        store, _h, manifest, _r, p, _g, pin = self.ready_many()
        best = manifest['members'][0]['blob_sha']
        self.damage('vectors', 'UPDATE vectors SET provenance_digest=? WHERE blob_sha=?',
                    ('0' * 64, best))
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'embedding_set_invalid')

    def test_all_rows_are_checked_for_invalid_values_even_below_top_sixty(self):
        store, _h, manifest, _r, p, _g, pin = self.ready_many()
        last = manifest['members'][-1]['blob_sha']
        self.damage('vectors', 'UPDATE vectors SET embedding=? WHERE blob_sha=?',
                    (struct.pack('<2f', float('nan'), 1.), last))
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'embedding_values_invalid')

    def test_bulk_read_counts_lengths_and_sequences_are_validated(self):
        store, _h, manifest, _r, p, _g, pin = self.ready_many(2)
        self.damage('vectors', 'UPDATE vectors SET embedding=? WHERE blob_sha=?',
                    (b'bad!', manifest['members'][0]['blob_sha']))
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'embedding_set_invalid')

    def test_bulk_read_sequence_mismatch_refuses(self):
        store, _h, manifest, _r, p, _g, pin = self.ready_many(2)
        self.damage('vectors', 'UPDATE vectors SET seq=? WHERE blob_sha=?',
                    (9, manifest['members'][0]['blob_sha']))
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'embedding_set_invalid')

    def test_missing_vector_below_candidate_cutoff_refuses(self):
        store, _h, manifest, _r, p, _g, pin = self.ready_many()
        self.damage('vectors', 'DELETE FROM vectors WHERE blob_sha=?',
                    (manifest['members'][-1]['blob_sha'],))
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'embedding_set_invalid')

    def test_multi_chunk_card_uses_max_cosine_and_excludes_unpublished_member(self):
        store, _repo, h, manifest, r, p = self.configured(3)
        for i, member in enumerate(manifest['members']):
            body = store.document(member['blob_sha'])['body']
            cut = len(body) // 2
            self.put_vectors(store, member['blob_sha'], r, p,
                             [[-1., 0.], [1., i + .5]],
                             pieces=[support.chunk(body[:cut]),
                                     support.chunk(body[cut:], seq=1, position=cut)])
        _gen, pin = self.publish(store, h, manifest, r, p)
        self.addCleanup(store.release, pin)
        self.add_member(store, h, manifest, 'cards/unpublished.md', b'# New\nnever published\n', 'New')
        self.put_vectors(store, manifest['members'][-1]['blob_sha'], r, p, [[1., 0.]])
        with mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}):
            expected = store.search_exact(pin, [1., 0.], 20, profile_id=p)
        stats = {}
        self.assertEqual(store.search_exact(pin, [1., 0.], 20, profile_id=p, timings=stats), expected)
        self.assertEqual(len(expected), 3)
        self.assertEqual(stats['vectorRows'], 6)

    def test_shared_blob_and_ties_preserve_path_order_and_pinned_generation(self):
        store, h, manifest, r, p, _g, pin = self.ready_many(2)
        body = store.document(manifest['members'][0]['blob_sha'])['body'].encode()
        self.add_member(store, h, manifest, 'cards/alias.md', body, 'Alias')
        _gen, fresh = self.publish(store, h, manifest, r, p)
        self.addCleanup(store.release, fresh)
        with mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}):
            expected = store.search_exact(fresh, [1., 0.], 20, profile_id=p)
        stats = {}
        actual = store.search_exact(fresh, [1., 0.], 20, profile_id=p, timings=stats)
        self.assertEqual(actual, expected)
        self.assertEqual(stats['vectorRows'], 2)
        self.assertEqual(len(actual), 3)
        self.assertEqual(self.refused(store.search_exact, pin, [1., 0.], 20,
                                     profile_id=p).reason, 'view_head_changed')
