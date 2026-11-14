"""Synthetic schema3 growth/work observations, never operational performance proof."""
import copy
import json
from pathlib import Path
import time
import unittest

from memvane.store.tests import support as sets
from memvane.store.tests import support as vectors

DIMENSION = 768
QUERY = [1.0] + [0.0] * (DIMENSION - 1)


class VectorBudgetChecks(vectors.VectorCase):
    two_chunks = False

    def configured_budget(self):
        store, _, handle = self.seeded()
        manifest = self.snapshot(store, handle, body=b'# Budget zero\nhello synthetic card zero\n')
        for i in range(1, 12):
            self.add_member(store, handle, manifest, f'cards/{i:02d}.md',
                            f'# Budget {i}\nhello synthetic card {i}\n'.encode(), f'Budget {i}')
        profile = dict(sets.profile(), dimension=DIMENSION)
        return (store, handle, manifest, store.register_recipe(sets.recipe()),
                store.register_profile(profile))

    def callbacks(self, store):
        def chunker(document, recipe):
            self.assertFalse(store._db.in_transaction)
            self.assertTrue(self.calls['fence'])
            self.assertEqual(document, store.document(Path(document['path']).stem))
            self.assertEqual(recipe, sets.recipe())
            self.calls['chunker'].append(copy.deepcopy(document))
            body = document['body']
            if self.two_chunks:
                cut = len(body) // 2  # Every body in this fixture is ASCII.
                return [sets.chunk(body[:cut]), sets.chunk(body[cut:], seq=1, position=cut)]
            return [sets.chunk(body)]

        def embedder(inputs, profile):
            self.assertFalse(store._db.in_transaction)
            self.assertEqual(profile['dimension'], DIMENSION)
            self.assertTrue(all(isinstance(value, str) for value in inputs))
            self.calls['embedder'].append(list(inputs))
            return [list(QUERY) for _ in inputs]

        def fence():
            self.assertFalse(store._db.in_transaction)
            self.calls['fence'].append(True)

        return dict(chunker=chunker, embedder=embedder, write_fence=fence)

    def measure(self, fn, *args, **kwargs):
        started = time.perf_counter_ns()
        result = fn(*args, **kwargs)
        return result, (time.perf_counter_ns() - started) / 1_000_000

    def inventory(self, store):
        counts = store.counts()
        page_count = store._db.execute('PRAGMA page_count').fetchone()[0]
        page_size = store._db.execute('PRAGMA page_size').fetchone()[0]
        return dict(logical_database_bytes=page_count * page_size,
            page_count=page_count, page_size=page_size,
            counts={key: counts[key] for key in ('wiki_blobs', 'chunk_sets', 'chunks',
                                                'embedding_sets', 'vectors')},
            packed_vector_bytes=store._db.execute(
                'SELECT COALESCE(SUM(length(embedding)),0) FROM vectors').fetchone()[0],
            complete_generations=store._db.execute(
                "SELECT COUNT(*) FROM s_generations WHERE coverage='complete'").fetchone()[0],
            open_eligible_cards=store._db.execute(
                'SELECT COUNT(*) FROM s_cards WHERE gen_to IS NULL AND eligible=1').fetchone()[0],
            provider_calls=len(self.calls['embedder']),
            provider_input_rows=sum(len(rows) for rows in self.calls['embedder']))

    def fill_all(self, store, handle, manifest, recipe, profile):
        result, elapsed = self.measure(self.fill, store, handle, manifest, recipe, profile,
                                       max_files=64)
        self.assert_status(result, len([m for m in manifest['members'] if m['trust_digest']]), True)
        return dict(result=result, milliseconds=elapsed)

    def test_01_rename_reuses_vector_bytes_and_profile_adds_complete_sets(self):
        # Format 4: lexical head first, the fill over it, then the complete
        # generation; no per-generation vector reference rows exist.
        store, handle, manifest, recipe, profile = self.configured_budget()
        self.lexical(store, handle, manifest)
        states = dict(before_fill=self.inventory(store))
        fill = self.fill_all(store, handle, manifest, recipe, profile)
        states['after_fill'] = self.inventory(store)
        first, old_pin = self.publish(store, handle, manifest, recipe, profile)
        self.addCleanup(store.release, old_pin)
        states['after_first_publish'] = self.inventory(store)
        self.assertEqual(states['after_fill']['counts'], dict(
            wiki_blobs=12, chunk_sets=12, chunks=12, embedding_sets=12, vectors=12))
        self.assertEqual(states['after_fill']['packed_vector_bytes'], 12 * DIMENSION * 4)
        self.assertEqual(states['after_fill']['provider_calls'], 12)
        self.assertEqual((states['after_first_publish']['complete_generations'],
                          states['after_first_publish']['open_eligible_cards']), (1, 12))
        old_rows = store.search_exact(old_pin, QUERY, 100, profile_id=profile)
        body = store.document(manifest['members'][0]['blob_sha'])['body'].encode()
        self.add_member(store, handle, manifest, 'renamed/same-content.md', body, 'Renamed')
        self.lexical(store, handle, manifest)
        renamed_fill = self.fill_all(store, handle, manifest, recipe, profile)
        states['after_rename_fill'] = self.inventory(store)
        for field in ('counts', 'packed_vector_bytes', 'provider_calls', 'provider_input_rows'):
            self.assertEqual(states['after_rename_fill'][field], states['after_fill'][field])
        self.assertEqual(renamed_fill['result']['filled'], 0)
        second, renamed_pin = self.publish(store, handle, manifest, recipe, profile)
        self.addCleanup(store.release, renamed_pin)
        states['after_rename_publish'] = self.inventory(store)
        self.assertEqual((states['after_rename_publish']['complete_generations'],
                          states['after_rename_publish']['open_eligible_cards']), (2, 13))
        self.assertEqual(len(store.search_exact(renamed_pin, QUERY, 100, profile_id=profile)), 13)
        self.assertNotEqual(first, second)
        self.assertEqual(len(old_rows), 12)
        self.assertEqual(self.refused(store.search_exact, old_pin, QUERY, 100, profile_id=profile).reason,
                         'view_head_changed')
        # The complete generation is a new head: one cache-hit pass rebinds the
        # cursor to it, and a repeat over the same head writes nothing.
        rebound = self.fill_all(store, handle, manifest, recipe, profile)
        self.assertEqual(rebound['result']['filled'], 0)
        changes = store._db.total_changes
        replay = self.fill_all(store, handle, manifest, recipe, profile)
        states['after_completed_replay'] = self.inventory(store)
        self.assertEqual(store._db.total_changes, changes)
        self.assertEqual(states['after_completed_replay'], states['after_rename_publish'])
        other = dict(sets.profile(), dimension=DIMENSION, model_sha='8' * 64)
        profile2 = store.register_profile(other)
        profile_fill = self.fill_all(store, handle, manifest, recipe, profile2)
        states['after_profile_fill'] = self.inventory(store)
        self.assertEqual(states['after_profile_fill']['counts'], dict(
            wiki_blobs=12, chunk_sets=12, chunks=12, embedding_sets=24, vectors=24))
        self.assertEqual(states['after_profile_fill']['provider_calls'], 24)
        self.assertEqual(states['after_profile_fill']['packed_vector_bytes'], 24 * DIMENSION * 4)
        complete = store._db.execute(
            'SELECT COUNT(*),SUM(chunk_count) FROM embedding_sets WHERE profile_id=?',
            (profile2,)).fetchone()
        self.assertEqual(tuple(complete), (12, 12))
        _, newer_pin = self.publish(store, handle, manifest, recipe, profile2)
        self.addCleanup(store.release, newer_pin)
        states['after_profile_publish'] = self.inventory(store)
        self.assertEqual((states['after_profile_publish']['complete_generations'],
                          states['after_profile_publish']['open_eligible_cards']), (3, 13))
        self.assertEqual(len(store.search_exact(newer_pin, QUERY, 100, profile_id=profile2)), 13)
        print(json.dumps(dict(case='synthetic_growth', dimension=DIMENSION,
            evidence='commitment-only', owner_verified=False, states=states,
            logical_growth_bytes=states['after_profile_publish']['logical_database_bytes']
                - states['before_fill']['logical_database_bytes'],
            fill=fill, rename_fill=renamed_fill, completed_replay=replay,
            profile_fill=profile_fill), sort_keys=True))

    def test_02_filtered_chunk_dimension_budget_preserves_lexical_on_defer(self):
        self.two_chunks = True
        store, handle, manifest, recipe, profile = self.configured_budget()
        body = store.document(manifest['members'][0]['blob_sha'])['body'].encode()
        tail = 'public/same-content.md'
        self.add_member(store, handle, manifest, tail, body, 'Public')
        self.add_member(store, handle, manifest, 'hidden/untrusted.md',
                        b'# Untrusted\nhello synthetic hidden\n', 'Untrusted')
        manifest['members'][-1]['trust_digest'] = None
        self.lexical(store, handle, manifest)
        fill = self.fill_all(store, handle, manifest, recipe, profile)
        gen, pin = self.publish(store, handle, manifest, recipe, profile)
        self.addCleanup(store.release, pin)
        work_sql = ('SELECT COALESCE(SUM(e.chunk_count*p.dimension),0) '
                    'FROM s_cards c JOIN s_generations g ON g.view_id=c.view_id AND g.gen_id=? '
                    'JOIN embedding_sets e ON e.blob_sha=c.blob_sha AND e.recipe_id=g.recipe_id '
                    'AND e.profile_id=g.profile_id JOIN vector_profiles p ON p.profile_id=e.profile_id '
                    'WHERE c.gen_from<=g.ordinal AND (c.gen_to IS NULL OR c.gen_to>g.ordinal) AND c.eligible=1')
        total_work = store._db.execute(work_sql, (gen,)).fetchone()[0]
        filtered_work = store._db.execute(work_sql + ' AND c.path=?', (gen, tail)).fetchone()[0]
        self.assertEqual((total_work, filtered_work), (13 * 2 * DIMENSION, 2 * DIMENSION))
        all_rows, full_ms = self.measure(store.search_exact, pin, QUERY, 100,
                                         profile_id=profile, max_float_work=total_work)
        self.assertEqual(len(all_rows), 13)
        self.assert_rows(all_rows, pin)
        self.bounded_refusal(store.search_exact, pin, QUERY, 1,
                             profile_id=profile, max_float_work=filtered_work)
        measurements = []
        for filters in (dict(path_prefixes=['public']),
                        dict(excluded_paths=[m['path'] for m in manifest['members'] if m['path'] != tail])):
            rows, exact_ms = self.measure(store.search_exact, pin, QUERY, 1,
                profile_id=profile, max_float_work=filtered_work, **filters)
            self.assertEqual([row['path'] for row in rows], [tail])
            self.assert_rows(rows, pin)
            error = self.bounded_refusal(store.search_exact, pin, QUERY, 1,
                profile_id=profile, max_float_work=filtered_work - 1, **filters)
            lexical = store.search(pin, 'hello', 10, **filters)
            deferred, hybrid_ms = self.measure(store.search_hybrid, pin, 'hello', QUERY, 10,
                profile_id=profile, max_float_work=filtered_work - 1, **filters)
            self.assertTrue(lexical)
            self.assertEqual(deferred['lexical'], lexical)
            self.assertEqual(deferred['semantic'], [])
            self.assertIn('defer', deferred['semantic_status'])
            absent = store.search_hybrid(pin, 'hello', None, 10, **filters)
            self.assertEqual(absent['lexical'], lexical)
            self.assertEqual(absent['semantic'], [])
            recovered = store.search_hybrid(pin, 'hello', QUERY, 10,
                profile_id=profile, max_float_work=filtered_work, **filters)
            self.assertEqual(recovered['lexical'], lexical)
            self.assertEqual([row['path'] for row in recovered['semantic']], [tail])
            measurements.append(dict(filters=filters, exact_milliseconds=exact_ms,
                hybrid_milliseconds=hybrid_ms, threshold_refusal=error.reason,
                semantic_status=deferred['semantic_status'],
                absent_status=absent['semantic_status'], lexical_paths=[r['path'] for r in lexical]))
        print(json.dumps(dict(case='synthetic_filtered_work', dimension=DIMENSION,
            chunks_per_blob=2, total_float_work=total_work, filtered_float_work=filtered_work,
            full_exact_milliseconds=full_ms, inventory=self.inventory(store), fill=fill,
            evidence='commitment-only', owner_verified=False, measurements=measurements), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
