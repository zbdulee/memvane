"""Sealed complete vector coverage and pinned exact lanes; no model/provider IO."""
import copy
from contextlib import closing
from dataclasses import replace
import importlib.util
from pathlib import Path
import sqlite3
import struct
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('vector_fill_fixture', HERE / 'laya-view-fill-checks.py')
fill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fill)
sets = fill.sets


class VectorCase(fill.FillCase):
    pair = fill.generation.GenerationChecks.pair

    def setUp(self):
        super().setUp()
        for name in ('search_exact', 'search_hybrid'):
            self.assertTrue(callable(getattr(self.Store, name, None)),
                            'PR4 ViewStore.' + name + ' API is absent')

    def publish(self, store, handle, manifest, recipe_id=None, profile_id=None):
        base = store.head(handle)
        vector_spec = None if recipe_id is None else dict(recipe_id=recipe_id, profile_id=profile_id)
        gen = store.prepare_generation(handle, manifest, expected_head=base, vector_spec=vector_spec)
        store.publish_generation(handle, gen, expected_head=base)
        return gen, store.pin_view(handle)

    def ready(self, count=1, *, lexical=False):
        store, repository, h, manifest, r, p = self.configured(count, body=b'# Alpha\nhello original snapshot\n')
        if not lexical:
            self.fill(store, h, manifest, r, p)
        gen, pin = self.publish(store, h, manifest, None if lexical else r, p)
        return store, repository, h, manifest, r, p, gen, pin

    def put_vectors(self, store, blob, recipe_id, profile_id, values, *, pieces=None):
        body = store.document(blob)['body']
        chunks = pieces or [sets.chunk(body)]
        store.put_chunk_set(blob, recipe_id, chunks)
        store.put_embedding_set(blob, recipe_id, profile_id,
            [dict(seq=i, input_sha=store.vector_input_sha(blob, recipe_id, profile_id, i),
                  embedding=struct.pack('<' + 'f' * len(value), *value))
             for i, value in enumerate(values)])

    def assert_rows(self, rows, pin):
        for row in rows:
            self.assertEqual((row['gen_id'], row['view_id']), (pin.gen_id, pin.view_id))
            self.assertEqual(row['evidence'], 'commitment-only')
            self.assertIs(row['owner_verified'], False)
            self.assertTrue(isinstance(row['body'], str))
            self.assertTrue(isinstance(row['score'], (int, float)))

    def damage(self, table, sql, params=()):
        """Alter existing private fixture relations, then restore exact schema SQL."""
        with closing(sqlite3.connect(self.db)) as db, db:
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                                  (table,)).fetchall()
            self.assertTrue(triggers, 'fixture damage requires existing immutable triggers')
            for name, _ in triggers:
                db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            db.execute(sql, params)
            for _, ddl in triggers:
                db.execute(ddl)


class VectorChecks(VectorCase):
    def test_reader_factory_preserves_exact_and_hybrid_results(self):
        store,repository,handle,manifest,recipe,profile,_gen,pin=self.ready()
        expected_exact=store.search_exact(pin,[1.0,0.0],10,profile_id=profile)
        expected_hybrid=store.search_hybrid(pin,'hello',[1.0,0.0],10,profile_id=profile)
        reader=self.Store.open_reader(store._path,repository=repository,scope_id=store._scope)
        self.addCleanup(reader.close)
        read_handle=reader.capture_view(handle.worktree_id)
        read_pin=reader.pin_view(read_handle)
        self.assertEqual(reader.search_exact(read_pin,[1.0,0.0],10,profile_id=profile),expected_exact)
        self.assertEqual(reader.search_hybrid(read_pin,'hello',[1.0,0.0],10,profile_id=profile),expected_hybrid)
        self.assertEqual(reader._db.execute('PRAGMA query_only').fetchone()[0],1)
        reader.release(read_pin)
        filler=importlib.import_module('laya_blob_fill').ContentFiller(reader)
        leases=importlib.import_module('laya_blob_lease')
        blob=manifest['members'][0]['blob_sha']
        calls=[]
        with mock.patch.object(leases,'acquire',side_effect=AssertionError('unexpected lock')):
            with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
                with filler.claim_content(blob,recipe,profile):
                    self.fail('reader granted content lease')
            with self.assertRaisesRegex(ValueError,'reader_write_forbidden'):
                filler.ensure_content(blob,recipe,profile,chunker=lambda *_:calls.append('chunk'),
                    embedder=lambda *_:calls.append('embed'),write_fence=lambda:calls.append('fence'))
        self.assertEqual(calls,[])

    def test_01_lexical_default_seals_zero_vector_coverage(self):
        store, _, h, manifest, r, p = self.configured(body=b'# Alpha\nhello lexical\n')
        self.fill(store, h, manifest, r, p)
        base = store.head(h)
        gen = store.prepare_generation(h, manifest, expected_head=base)
        self.assertEqual(store._db.execute('SELECT vector_member_count FROM generation_seals WHERE gen_id=?',
                                         (gen,)).fetchone()[0], 0)
        self.assertEqual(store._db.execute('SELECT COUNT(*) FROM member_vectors WHERE gen_id=?',
                                         (gen,)).fetchone()[0], 0)
        store.publish_generation(h, gen, expected_head=base)
        pin = store.pin_view(h)
        self.assertEqual(len(store.search(pin, 'hello', 10)), 1)
        result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p)
        self.assertEqual(result['semantic'], [])
        self.assertEqual(result['lexical'], store.search(pin, 'hello', 10))

    def test_02_full_coverage_pins_members_and_deterministic_cosine_order(self):
        store, _, h, manifest, r, p = self.configured(3)
        for member in manifest['members']:
            self.put_vectors(store, member['blob_sha'], r, p, [[1.0, 0.0]])
        gen, pin = self.publish(store, h, manifest, r, p)
        self.assertEqual(store._db.execute('SELECT vector_member_count FROM generation_seals WHERE gen_id=?',
                                         (gen,)).fetchone()[0], 3)
        rows = store.search_exact(pin, [1.0, 0.0], 10, profile_id=p)
        self.assertEqual([row['path'] for row in rows], sorted(row['path'] for row in manifest['members']))
        self.assert_rows(rows, pin)
        for row in rows:
            self.assertAlmostEqual(row['score'], 1.0, places=6)
            self.assertEqual(row['blob_sha'], next(m['blob_sha'] for m in manifest['members']
                                                   if m['path'] == row['path']))

    def test_03_missing_member_set_never_publishes_partial_coverage(self):
        store, _, h, manifest, r, p = self.configured(2)
        self.put_vectors(store, manifest['members'][0]['blob_sha'], r, p, [[1.0, 0.0]])
        head = store.head(h)
        self.bounded_refusal(store.prepare_generation, h, manifest, expected_head=head,
                             vector_spec=dict(recipe_id=r, profile_id=p))
        self.assertEqual(store.head(h), head)
        self.assertEqual(store.counts()['generations'], 0)

    def test_04_mixed_profile_sets_do_not_complete_one_profile(self):
        store, _, h, manifest, r, p = self.configured(2)
        other = sets.profile(); other['model_sha'] = '8' * 64
        p2 = store.register_profile(other)
        self.put_vectors(store, manifest['members'][0]['blob_sha'], r, p, [[1.0, 0.0]])
        self.put_vectors(store, manifest['members'][1]['blob_sha'], r, p2, [[1.0, 0.0]])
        for profile_id in (p, p2):
            with self.subTest(profile=profile_id):
                self.bounded_refusal(store.prepare_generation, h, manifest, expected_head=store.head(h),
                                     vector_spec=dict(recipe_id=r, profile_id=profile_id))
        self.assertIsNone(store.head(h)['gen_id'])

    def test_05_set_digest_spoof_refuses_before_publish_and_after_pin(self):
        store, _, h, manifest, r, p, _, pin = self.ready()
        self.damage('embedding_sets', 'UPDATE embedding_sets SET set_digest=?', ('f' * 64,))
        self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 10, profile_id=p)
        self.bounded_refusal(store.search_hybrid, pin, 'hello', [1.0, 0.0], 10, profile_id=p)
        self.bounded_refusal(store.prepare_generation, h, manifest, expected_head=store.head(h),
                             vector_spec=dict(recipe_id=r, profile_id=p))

    def test_06_member_vector_rows_are_immutable_and_actual_rows_are_sealed(self):
        store, _, _, _, _, p, gen, pin = self.ready()
        with closing(sqlite3.connect(self.db)) as db, db:
            with self.assertRaises(sqlite3.Error):
                db.execute('DELETE FROM member_vectors WHERE gen_id=?', (gen,))
            with self.assertRaises(sqlite3.Error):
                db.execute('INSERT OR REPLACE INTO member_vectors SELECT * FROM member_vectors WHERE gen_id=?', (gen,))
        self.damage('member_vectors', 'DELETE FROM member_vectors WHERE gen_id=?', (gen,))
        self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 10, profile_id=p)
        self.bounded_refusal(store.search_hybrid, pin, 'hello', None, 10)

    def test_07_later_shared_cache_growth_cannot_upgrade_an_old_lexical_pin(self):
        store, _, h, manifest, r, p, old_gen, pin = self.ready(lexical=True)
        seal = tuple(store._db.execute('SELECT * FROM generation_seals WHERE gen_id=?', (old_gen,)).fetchone())
        lexical = store.search(pin, 'hello', 10)
        self.fill(store, h, manifest, r, p)
        self.assertEqual(tuple(store._db.execute('SELECT * FROM generation_seals WHERE gen_id=?',
                                                (old_gen,)).fetchone()), seal)
        result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p)
        self.assertEqual(result['semantic'], [])
        self.assertEqual(result['lexical'], lexical)
        _, fresh = self.publish(store, h, manifest, r, p)
        self.assertEqual(len(store.search_exact(fresh, [1.0, 0.0], 10, profile_id=p)), 1)
        self.assertEqual(store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p)['semantic'], [])

    def test_08_head_swap_preserves_old_vector_body_and_profile_binding(self):
        store, _, h, _, r, p, first, old = self.ready()
        manifest = self.snapshot(store, h, scan_id='2' * 32, body=b'# New\nhello replacement snapshot\n')
        self.fill(store, h, manifest, r, p)
        second, fresh = self.publish(store, h, manifest, r, p)
        a = store.search_exact(old, [1.0, 0.0], 10, profile_id=p)
        b = store.search_exact(fresh, [1.0, 0.0], 10, profile_id=p)
        self.assertEqual({row['gen_id'] for row in a}, {first})
        self.assertEqual({row['gen_id'] for row in b}, {second})
        self.assertIn('original snapshot', a[0]['body'])
        self.assertIn('replacement snapshot', b[0]['body'])
        self.assert_rows(a, old); self.assert_rows(b, fresh)

    def test_09_other_view_head_and_rows_remain_independent(self):
        store, main, linked, a, b = self.pair('vector-views')
        ma = self.snapshot(store, a, root=main)
        mb = self.snapshot(store, b, root=linked)
        r = store.register_recipe(sets.recipe()); p = store.register_profile(sets.profile())
        self.fill(store, a, ma, r, p)
        self.fill(store, b, mb, r, p)
        _, pb = self.publish(store, b, mb, r, p)
        before = store.head(b)
        # This case checks view isolation, not latency. Deadline behavior is
        # covered separately; the product default remains two seconds.
        gb = store.search_exact(pb, [1.0, 0.0], 10, profile_id=p, deadline_seconds=10)
        _, pa = self.publish(store, a, ma, r, p)
        self.assertEqual(store.head(b), before)
        self.assertEqual(store.search_exact(pb, [1.0, 0.0], 10, profile_id=p, deadline_seconds=10), gb)
        self.assert_rows(gb, pb)
        self.assert_rows(store.search_exact(pa, [1.0, 0.0], 10, profile_id=p, deadline_seconds=10), pa)
        self.assertEqual(len(self.calls['embedder']), 1, 'two views share immutable content, not publication')

    def test_10_path_and_eligibility_filters_precede_float_work_budget(self):
        store, _, h, manifest, r, p = self.configured()
        self.add_member(store, h, manifest, 'private/b.md', b'# B\nhello private\n', 'B')
        self.add_member(store, h, manifest, 'public/c.md', b'# C\nhello public\n', 'C')
        self.add_member(store, h, manifest, 'hidden/untrusted.md', b'# Untrusted\nhello hidden\n', 'Untrusted')
        manifest['members'][-1]['trust_digest'] = None
        self.assert_status(self.fill(store, h, manifest, r, p), 3, True)
        _, pin = self.publish(store, h, manifest, r, p)
        all_rows = store.search_exact(pin, [1.0, 0.0], 10, profile_id=p, max_float_work=6)
        self.assertEqual(len(all_rows), 3, 'ineligible member is excluded before work calculation')
        self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 1, profile_id=p, max_float_work=2)
        for filters in [dict(path_prefixes=['public']),
                        dict(excluded_paths=['cards/a.md', 'private/b.md'])]:
            rows = store.search_exact(pin, [1.0, 0.0], 1, profile_id=p, max_float_work=2, **filters)
            self.assertEqual([row['path'] for row in rows], ['public/c.md'])
        self.assertEqual(store.search_exact(pin, [1.0, 0.0], 1, profile_id=p,
                                            max_float_work=1, path_prefixes=[]), [])

    def test_11_work_counts_every_chunk_times_dimension_with_exact_boundary(self):
        store, _, h, manifest, r, p = self.configured()
        blob = manifest['members'][0]['blob_sha']; body = store.document(blob)['body']
        pieces = [sets.chunk(body[:4]), sets.chunk(body[4:], seq=1, position=4)]
        self.put_vectors(store, blob, r, p, [[1.0, 0.0], [0.0, 1.0]], pieces=pieces)
        _, pin = self.publish(store, h, manifest, r, p)
        self.assertEqual(len(store.search_exact(pin, [1.0, 0.0], 1, profile_id=p, max_float_work=4)), 1)
        self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 1, profile_id=p, max_float_work=3)

    def test_12_invalid_query_profile_pin_and_spec_refuse_instead_of_degrading(self):
        store, repository, h, manifest, r, p, _, pin = self.ready()
        for query in ([1.0], [1.0, 0.0, 1.0], [0.0, 0.0], [float('nan'), 1.0],
                      [float('inf'), 1.0], ['1', 0.0], [True, 1.0]):
            with self.subTest(query=query):
                self.bounded_refusal(store.search_exact, pin, query, 1, profile_id=p)
                self.bounded_refusal(store.search_hybrid, pin, 'hello', query, 1, profile_id=p)
        for kwargs in (dict(max_float_work=0), dict(max_float_work=True),
                       dict(deadline_seconds=0), dict(deadline_seconds=float('inf')),
                       dict(deadline_seconds=10**1000),
                       dict(path_prefixes=['../bad']), dict(excluded_paths='cards/a.md')):
            with self.subTest(kwargs=kwargs):
                self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 1, profile_id=p, **kwargs)
                self.bounded_refusal(store.search_hybrid, pin, 'hello', [1.0, 0.0], 1, profile_id=p, **kwargs)
        other = sets.profile(); other['model_sha'] = '9' * 64
        p2 = store.register_profile(other)
        for fn, args in [(store.search_exact, ([1.0, 0.0], 1)),
                         (store.search_hybrid, ('hello', [1.0, 0.0], 1))]:
            self.bounded_refusal(fn, pin, *args, profile_id=p2)
            self.bounded_refusal(fn, replace(pin, epoch=pin.epoch + 1), *args, profile_id=p)
        other_store = self.reopen(repository)
        self.bounded_refusal(other_store.search_exact, pin, [1.0, 0.0], 1, profile_id=p)
        for vector_spec in ({}, dict(recipe_id=r), dict(recipe_id=r, profile_id=p, extra=True)):
            self.bounded_refusal(store.prepare_generation, h, manifest, expected_head=store.head(h),
                                 vector_spec=vector_spec)
        store.release(pin)
        self.bounded_refusal(store.search_exact, pin, [1.0, 0.0], 1, profile_id=p)

    def test_13_absent_query_provider_preserves_lexical_and_profile_can_be_inferred(self):
        store, _, _, _, _, p, _, pin = self.ready()
        lexical = store.search(pin, 'hello', 10)
        absent = store.search_hybrid(pin, 'hello', None, 10)
        self.assertEqual(absent['lexical'], lexical)
        self.assertEqual(absent['semantic'], [])
        self.assertTrue(isinstance(absent['semantic_status'], str) and absent['semantic_status'])
        inferred = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10)
        explicit = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p)
        self.assertEqual(inferred['semantic'], explicit['semantic'])
        self.assertEqual(inferred['lexical'], lexical)

    def test_14_semantic_budget_defer_preserves_same_pinned_lexical_lane(self):
        store, _, _, _, _, p, _, pin = self.ready(2)
        lexical = store.search(pin, 'hello', 10)
        result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p, max_float_work=3)
        self.assertEqual(result['lexical'], lexical)
        self.assertEqual(result['semantic'], [])
        self.assertIn('defer', result['semantic_status'])
        self.assert_rows(result['lexical'], pin)
        recovered = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10, profile_id=p, max_float_work=4)
        self.assertEqual(len(recovered['semantic']), 2)

    def test_15_hybrid_uses_one_deadline_and_keeps_completed_lexical_on_expiry(self):
        store, _, _, _, _, p, _, pin = self.ready()
        original = store.search
        baseline = original(pin, 'hello', 10)
        for elapsed in (0.25, 3.0):
            with self.subTest(elapsed=elapsed):
                clock = [time.monotonic()]; passed = []
                def lexical(*args, **kwargs):
                    rows = original(*args, **kwargs)
                    clock[0] += elapsed
                    return rows
                def semantic(*args, **kwargs):
                    passed.append(kwargs['deadline_seconds'])
                    return []
                with mock.patch.object(store, 'search', side_effect=lexical), \
                     mock.patch.object(store, 'search_exact', side_effect=semantic), \
                     mock.patch('time.monotonic', side_effect=lambda: clock[0]):
                    result = store.search_hybrid(pin, 'hello', [1.0, 0.0], 10,
                                                 profile_id=p, deadline_seconds=2)
                self.assertEqual(result['lexical'], baseline)
                if elapsed > 2:
                    self.assertEqual(passed, [], 'expired shared deadline prevents semantic dispatch')
                    self.assertEqual(result['semantic'], [])
                    self.assertIn('defer', result['semantic_status'])
                else:
                    self.assertEqual(len(passed), 1)
                    self.assertGreater(passed[0], 0)
                    self.assertLessEqual(passed[0], 2 - elapsed, 'semantic receives remaining time, not a new budget')


    def test_16_query_vector_keyword_matches_the_public_api(self):
        store, _, _, _, _, p, _, pin = self.ready()
        self.assertEqual(store.search_exact(pin, query_vector=[1, 0], limit=10, profile_id=p),
                         store.search_exact(pin, [1, 0], 10, profile_id=p))
        self.assertEqual(store.search_hybrid(pin, 'hello', query_vector=[1, 0], limit=10, profile_id=p),
                         store.search_hybrid(pin, 'hello', [1, 0], 10, profile_id=p))

    def test_17_publish_revalidates_vector_bytes_changed_after_prepare(self):
        store, _, h, manifest, r, p = self.configured()
        self.fill(store, h, manifest, r, p)
        base = store.head(h)
        gen = store.prepare_generation(h, manifest, expected_head=base,
                                       vector_spec=dict(recipe_id=r, profile_id=p))
        self.damage('vectors', 'UPDATE vectors SET embedding=?', (struct.pack('<ff', 0.5, 1.0),))
        self.bounded_refusal(store.publish_generation, h, gen, expected_head=base)
        self.assertEqual(store.head(h), base)

    def test_18_hybrid_lexical_receives_remaining_time_after_preflight(self):
        store, _, _, _, _, p, _, pin = self.ready()
        original = store._pinned
        for elapsed in (0.25, 3.0):
            clock, passed = [time.monotonic()], []
            def preflight(value):
                record = original(value)
                clock[0] += elapsed
                return record
            def lexical(*args, **kwargs):
                passed.append(kwargs['deadline_seconds'])
                return []
            with self.subTest(elapsed=elapsed), \
                 mock.patch.object(store, '_pinned', side_effect=preflight), \
                 mock.patch.object(store, 'search', side_effect=lexical), \
                 mock.patch('time.monotonic', side_effect=lambda: clock[0]):
                if elapsed >= 2:
                    self.bounded_refusal(store.search_hybrid, pin, 'hello', None, 10,
                                         profile_id=p, deadline_seconds=2)
                    self.assertEqual(passed, [])
                else:
                    store.search_hybrid(pin, 'hello', None, 10, profile_id=p, deadline_seconds=2)
                    self.assertEqual(len(passed), 1)
                    self.assertGreater(passed[0], 0)
                    self.assertLessEqual(passed[0], 2 - elapsed)


    def test_19_seal_revalidates_bytes_after_preflight_before_identity(self):
        store, _, h, manifest, r, p = self.configured()
        self.fill(store, h, manifest, r, p)
        base = store.head(h)
        observe, calls = store._observe, []
        def mutate_after_validation(entry):
            saved = observe(entry)
            calls.append(True)
            if len(calls) == 2:
                self.damage('vectors', 'UPDATE vectors SET embedding=?', (struct.pack('<ff', 0.5, 1),))
            return saved
        with mock.patch.object(store, '_observe', side_effect=mutate_after_validation):
            self.bounded_refusal(store.prepare_generation, h, manifest, expected_head=base,
                                 vector_spec=dict(recipe_id=r, profile_id=p))
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(store.head(h), base)
        self.assertEqual(store.counts()['generations'], 0)


if __name__ == '__main__':
    unittest.main()
