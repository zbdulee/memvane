"""Card exclusion by ``(path, blob sha)`` and exact search over several query vectors."""
from memvane.store.tests.support import VectorCase

A, B, C = [1.0, 0.0], [0.0, 1.0], [0.6, 0.8]


class ExcludedCards(VectorCase):
    def cards(self, vectors=(A, C, B)):
        """Three published cards whose single chunk vectors are ``vectors``, in member order."""
        store, _, handle, manifest, recipe, profile = self.configured(3)
        for member, value in zip(manifest['members'], vectors):
            self.put_vectors(store, member['blob_sha'], recipe, profile, [value])
        _, pin = self.publish(store, handle, manifest, recipe, profile)
        return store, pin, profile, [[m['path'], m['blob_sha']] for m in manifest['members']]

    def test_an_excluded_pair_leaves_both_lanes_and_the_next_card_takes_its_place(self):
        store, pin, profile, members = self.cards()
        ranked = [row['path'] for row in store.search_exact(pin, A, 3, profile_id=profile)]
        self.assertEqual(ranked, [members[0][0], members[1][0], members[2][0]])
        timings = {}
        rows = store.search_exact(pin, A, 2, profile_id=profile, excluded_cards=[members[0]], timings=timings)
        self.assertEqual([row['path'] for row in rows], ranked[1:])
        self.assertEqual(timings['vectorExcluded'], 1)
        timings = {}
        store.search_exact(pin, A, 2, profile_id=profile, timings=timings)
        self.assertEqual(timings['vectorExcluded'], 0)
        lexical = [row['path'] for row in store.search(pin, 'hello', 3)]
        self.assertEqual(len(lexical), 2)
        first = next(pair for pair in members if pair[0] == lexical[0])
        self.assertEqual([row['path'] for row in store.search(pin, 'hello', 1, excluded_cards=[first])], lexical[1:])
        both = store.search_hybrid(pin, 'hello', A, 3, profile_id=profile, excluded_cards=[first])
        self.assertNotIn(first[0], [row['path'] for lane in ('lexical', 'semantic') for row in both[lane]])
        self.assertEqual((len(both['lexical']), len(both['semantic'])), (1, 2))

    def test_a_pair_with_another_sha_excludes_nothing(self):
        store, pin, profile, members = self.cards()
        stale = [[members[0][0], 'f' * 64]]
        self.assertEqual(len(store.search_exact(pin, A, 3, profile_id=profile, excluded_cards=stale)), 3)
        self.assertEqual(len(store.search(pin, 'hello', 3, excluded_cards=[[members[1][0], 'f' * 64]])), 2)
        # Path exclusions and pair exclusions combine.
        rows = store.search(pin, 'hello', 3, excluded_paths=[members[1][0]], excluded_cards=[members[0]])
        self.assertEqual([row['path'] for row in rows], [members[2][0]])
        rows = store.search(pin, 'hello', 3, excluded_paths=[members[1][0]], excluded_cards=[members[2]])
        self.assertEqual(rows, [])

    def test_pair_lists_are_bounded_and_fixed_shape(self):
        store, pin, profile, members = self.cards()
        full = [['cards/x%03d.md' % index, '%064x' % index] for index in range(256)]
        self.assertEqual(len(store.search(pin, 'hello', 3, excluded_cards=full)), 2)
        self.assertEqual(len(store.search_exact(pin, A, 3, profile_id=profile, excluded_cards=full)), 3)
        for bad in (full + [['cards/y.md', 'a' * 64]], 'cards/a.md', [members[0][0]],
                    [[members[0][0], 'A' * 64]], [[members[0][0], 'a' * 63]], [[members[0][0], 7]],
                    [[members[0][0]]]):
            with self.subTest(bad=str(bad)[:40]):
                self.assertEqual(self.bounded_refusal(store.search, pin, 'hello', 3, excluded_cards=bad).reason,
                                 'fts_filter_invalid')
                self.assertEqual(self.bounded_refusal(store.search_exact, pin, A, 3, profile_id=profile,
                                                      excluded_cards=bad).reason, 'vector_filter_invalid')
                self.assertEqual(self.bounded_refusal(store.search_hybrid, pin, 'hello', A, 3, profile_id=profile,
                                                      excluded_cards=bad).reason, 'vector_filter_invalid')
        # A path outside the collection refuses like a path exclusion does.
        self.assertEqual(self.bounded_refusal(store.search, pin, 'hello', 3,
                                              excluded_cards=[['../x.md', 'a' * 64]]).reason, 'invalid_relative_path')


class SeveralQueryVectors(VectorCase):
    cards = ExcludedCards.cards

    def scores(self, store, pin, profile, query):
        return {row['path']: row['score'] for row in store.search_exact(pin, query, 3, profile_id=profile)}

    def test_each_card_keeps_its_best_score_over_the_query_vectors(self):
        store, pin, profile, _members = self.cards()
        single = [self.scores(store, pin, profile, query) for query in (A, B)]
        several = self.scores(store, pin, profile, [A, B])
        self.assertEqual(set(several), set(single[0]))
        for path, score in several.items():
            self.assertAlmostEqual(score, max(single[0][path], single[1][path]), places=6)
        ranked = [row['path'] for row in store.search_exact(pin, [A, B], 3, profile_id=profile)]
        self.assertEqual(ranked, sorted(several, key=lambda path: (-several[path], path.encode())))
        # One vector in a list is the same search as the bare vector.
        self.assertEqual(store.search_exact(pin, [A], 3, profile_id=profile),
                         store.search_exact(pin, A, 3, profile_id=profile))
        hybrid = store.search_hybrid(pin, 'hello', [A, B], 3, profile_id=profile)
        self.assertEqual([row['path'] for row in hybrid['semantic']], ranked)

    def test_the_python_lane_gives_the_same_scores(self):
        from memvane.store.view import vector_accel
        store, pin, profile, _members = self.cards()
        native = self.scores(store, pin, profile, [A, B, C])
        with self.subTest('python'):
            from unittest import mock
            with mock.patch.object(vector_accel, 'kernel', return_value=None):
                timings = {}
                rows = store.search_exact(pin, [A, B, C], 3, profile_id=profile, timings=timings)
            self.assertEqual(timings['vectorKernel'], 'python')
            for row in rows:
                self.assertAlmostEqual(row['score'], native[row['path']], places=6)
                self.assertAlmostEqual(row['score'], 1.0, places=6)

    def test_more_than_four_vectors_or_a_wrong_dimension_refuse(self):
        store, pin, profile, _members = self.cards()
        self.assertEqual(len(store.search_exact(pin, [A, B, C, A], 3, profile_id=profile)), 3)
        for query in ([A, B, C, A, B], [A, [1.0]], [A, [1.0, 0.0, 0.0]], [[]], [A, 'x'], [A, [0.0, 0.0]]):
            with self.subTest(query=query):
                self.assertEqual(self.bounded_refusal(store.search_exact, pin, query, 3, profile_id=profile).reason,
                                 'vector_query_invalid')
                self.assertEqual(self.bounded_refusal(store.search_hybrid, pin, 'hello', query, 3,
                                                      profile_id=profile).reason, 'vector_query_invalid')
