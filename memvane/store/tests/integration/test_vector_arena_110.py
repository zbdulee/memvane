"""Exact search over the vector arena: where each set is read, and that the answer does not change.

A small published store, an arena written beside it with the vectorcache
encoders, and the native kernel (darwin). Every check compares against the
same search with the arena switched off, or reads the reported fields
(``vectorSource``, ``vectorMissing``, ``vectorCache``).
"""
import json
import os
from pathlib import Path
import struct
import unittest
from unittest import mock
import zlib

from memvane.store.tests.support import VectorCase
from memvane.store.view import vector_accel, view_accel_search
from memvane.vectorcache import api as vectorcache
from memvane.vectorcache import types as arena_types

A, B, C = [1.0, 0.0], [0.0, 1.0], [0.6, 0.8]
NONCE = 'cd' * 16


def packed(*values):
    return b''.join(struct.pack('<2f', *value) for value in values)


@unittest.skipUnless(vector_accel.kernel() is not None, 'needs the native scan kernel')
class ArenaSearch(VectorCase):
    def setUp(self):
        super().setUp()
        # The baseline fixtures switch the native lane off; these cases are about it.
        native = mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': ''})
        native.start()
        self.addCleanup(native.stop)
        self.store, _, handle, manifest, self.recipe, self.profile = self.configured(3)
        self.vectors = dict(zip([member['blob_sha'] for member in manifest['members']], ([A, C], [C], [B])))
        for blob, values in self.vectors.items():
            body = self.store.document(blob)['body']
            pieces = None if len(values) == 1 else [self.chunk(body[:4]), self.chunk(body[4:], 1, 4)]
            self.put_vectors(self.store, blob, self.recipe, self.profile, values, pieces=pieces)
        _, self.pin = self.publish(self.store, handle, manifest, self.recipe, self.profile)
        self.paths = {member['blob_sha']: member['path'] for member in manifest['members']}
        self.directory = Path(self.store._path).parent / arena_types.DIRECTORY

    @staticmethod
    def chunk(text, seq=0, position=0):
        from memvane.store.tests import support
        return support.chunk(text, seq=seq, position=position)

    def digest(self, blob):
        return self.store._db.execute('SELECT set_digest FROM embedding_sets WHERE blob_sha=? AND recipe_id=? '
                                      'AND profile_id=?', (blob, self.recipe, self.profile)).fetchone()[0]

    def write_arena(self, sets, *, digests=None, crcs=None):
        """An arena of ``[(blob or None for a dead set, [vector, ...])]`` in that row order."""
        self.directory.mkdir(mode=0o700, exist_ok=True)
        value = vectorcache.identity(nonce=NONCE, profile_id=self.profile, recipe_id=self.recipe, dimension=2,
                                     store_incarnation=self.store._incarnation)
        vec, idx, first = vectorcache.vec_header(value), vectorcache.idx_header(value), 0
        for blob, values in sets:
            data = packed(*values)
            vec += data
            if blob is not None:
                idx += vectorcache.record(blob, first, len(values), (digests or {}).get(blob, self.digest(blob)),
                                          (crcs or {}).get(blob, zlib.crc32(data)))
            first += len(values)
        vec_name, idx_name = vectorcache.names(NONCE)
        for name, data in ((vec_name, vec), (idx_name, idx), (arena_types.CURRENT, json.dumps(value).encode())):
            (self.directory / name).write_bytes(data)
            os.chmod(self.directory / name, 0o600)

    def whole(self):
        return [(blob, values) for blob, values in self.vectors.items()]

    def search(self, query=A, limit=3, **options):
        timings = {}
        rows = self.store.search_exact(self.pin, query, limit, profile_id=self.profile, timings=timings, **options)
        return [(row['path'], row['score']) for row in rows], timings

    def from_store(self, query=A, limit=3, **options):
        with mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': 'store'}):
            return self.search(query, limit, **options)

    def reported(self, timings):
        return timings['vectorSource'], timings.get('vectorMissing'), timings['vectorCache']

    # -- where it reads -------------------------------------------------------------
    def test_without_an_arena_the_store_answers_as_before(self):
        rows, timings = self.search()
        self.assertEqual(self.reported(timings), ('store', None, 'absent'))
        self.assertEqual(len(rows), 3)

    def test_an_arena_that_covers_every_set_answers_alone_with_the_same_result(self):
        self.write_arena(self.whole())
        for query in (A, B, [A, B], [C, A, B, A]):
            with self.subTest(query=query):
                rows, timings = self.search(query)
                expected, plain = self.from_store(query)
                self.assertEqual(rows, expected)
                self.assertEqual(self.reported(timings), ('arena', 0, 'ok'))
                self.assertEqual(self.reported(plain), ('store', None, 'disabled'))

    def test_the_arena_scan_is_counted_as_scan_time_and_the_rest_as_load_time(self):
        self.write_arena(self.whole())
        ticks = iter(range(1000))
        # One second a reading: open and crc take one, the kernel scan one, the close one;
        # the candidate pass over the scores is one more of scan time.
        with mock.patch.object(view_accel_search, 'time', mock.Mock(perf_counter=lambda: next(ticks))):
            _rows, timings = self.search()
        self.assertEqual(self.reported(timings), ('arena', 0, 'ok'))
        self.assertEqual(timings['vectorScanMs'], 2000)
        self.assertEqual(int(timings['vectorLoadMs']) // 1000, 2)

    def test_a_deadline_inside_the_arena_scan_stays_a_deadline(self):
        from memvane.store.blob.blob_values import BlobStoreError
        self.write_arena(self.whole())
        native = vector_accel.kernel()

        def expired(buffer, *_args, **_kwargs):
            self.assertGreater(len(buffer), 0)  # The mapped rows are in the failing frame.
            raise BlobStoreError('vector_deadline_exceeded')
        # Inside the store read's bound and past it (a large install the arena covers).
        for bound in (view_accel_search.MAX_ROWS, 0):
            with self.subTest(bound=bound), mock.patch.object(view_accel_search, 'MAX_ROWS', bound), \
                    mock.patch.object(native, 'scores', side_effect=expired):
                timings = {}
                refusal = self.bounded_refusal(self.store.search_exact, self.pin, A, 3, profile_id=self.profile,
                                               timings=timings)
                self.assertEqual(refusal.reason, 'vector_deadline_exceeded')
                self.assertEqual(self.reported(timings), ('arena', None, 'ok'))
                answer = self.store.search_hybrid(self.pin, 'hello', A, 3, profile_id=self.profile)
                self.assertEqual((answer['semantic_status'], answer['semantic']), ('deferred_deadline', []))
        # Nothing is left broken: the next search answers from the arena.
        rows, timings = self.search()
        self.assertEqual((rows, self.reported(timings)), (self.from_store()[0], ('arena', 0, 'ok')))

    def test_sets_the_arena_does_not_hold_come_from_the_store(self):
        blobs = list(self.vectors)
        self.write_arena([(blobs[0], self.vectors[blobs[0]]), (blobs[2], self.vectors[blobs[2]])])
        rows, timings = self.search([A, B])
        self.assertEqual(rows, self.from_store([A, B])[0])
        self.assertEqual(self.reported(timings), ('arena', 1, 'building'))
        # An arena that holds none of this view's sets: the store answers, and the arena is building.
        self.write_arena([(None, [A, B])])
        rows, timings = self.search()
        self.assertEqual((rows, self.reported(timings)), (self.from_store()[0], ('store', None, 'building')))

    def test_only_the_store_read_is_bounded(self):
        blobs = list(self.vectors)
        # Four rows in all: an arena that covers the two-row set leaves two rows to the store.
        self.write_arena([(blobs[0], self.vectors[blobs[0]])])
        with mock.patch.object(view_accel_search, 'MAX_ROWS', 2):
            rows, timings = self.search()
            self.assertEqual(self.reported(timings), ('arena', 2, 'building'))
        self.assertEqual(rows, self.from_store()[0])
        with mock.patch.object(view_accel_search, 'MAX_ROWS', 1):
            self.assertEqual(self.bounded_refusal(self.store.search_exact, self.pin, A, 3,
                                                  profile_id=self.profile).reason, 'vector_cache_building')
            # Switched off on purpose, the same excess is the work budget's refusal.
            with mock.patch.dict(os.environ, {'MEMVANE_VECTOR_ACCEL': 'store'}):
                self.assertEqual(self.bounded_refusal(self.store.search_exact, self.pin, A, 3,
                                                      profile_id=self.profile).reason, 'vector_work_exceeded')
        # With every set in the arena no store row is read: no bound applies.
        self.write_arena(self.whole())
        with mock.patch.object(view_accel_search, 'MAX_ROWS', 0):
            rows, timings = self.search()
        self.assertEqual((len(rows), self.reported(timings)), (3, ('arena', 0, 'ok')))

    def test_without_an_arena_a_store_read_past_the_bound_waits_for_the_arena(self):
        with mock.patch.object(view_accel_search, 'MAX_ROWS', 3):
            self.assertEqual(self.bounded_refusal(self.store.search_exact, self.pin, A, 3,
                                                  profile_id=self.profile).reason, 'vector_cache_building')
        with mock.patch.object(view_accel_search, 'MAX_ROWS', 4):
            self.assertEqual(len(self.search()[0]), 3)

    # -- what is not believed ---------------------------------------------------------
    def test_a_record_that_does_not_hold_sends_only_that_set_to_the_store(self):
        blobs = list(self.vectors)
        for label, options in (('digest', {'digests': {blobs[1]: 'f' * 64}}), ('crc', {'crcs': {blobs[1]: 1}})):
            with self.subTest(label):
                self.write_arena(self.whole(), **options)
                rows, timings = self.search([A, B])
                self.assertEqual(rows, self.from_store([A, B])[0])
                self.assertEqual(self.reported(timings), ('arena', 1, 'inconsistent'))

    def test_a_zero_row_in_a_member_set_is_read_from_the_store_and_fails_nothing(self):
        blobs = list(self.vectors)
        sets = self.whole()
        sets[1] = (blobs[1], [[0.0, 0.0]])      # the crc is of these bytes: only the scan can tell
        self.write_arena(sets)
        rows, timings = self.search()
        self.assertEqual(rows, self.from_store()[0])
        self.assertEqual(self.reported(timings), ('arena', 1, 'inconsistent'))

    def test_a_zero_row_outside_the_member_sets_changes_nothing(self):
        blobs = list(self.vectors)
        sets = [(blobs[0], self.vectors[blobs[0]]), (None, [[0.0, 0.0]]), (blobs[1], self.vectors[blobs[1]]),
                (blobs[2], self.vectors[blobs[2]]), (None, [[0.0, 0.0], [0.0, 0.0]])]
        self.write_arena(sets)
        rows, timings = self.search([A, B])
        self.assertEqual(rows, self.from_store([A, B])[0])
        self.assertEqual(self.reported(timings), ('arena', 0, 'ok'))

    def test_arena_rows_that_score_unlike_the_store_are_overruled_by_the_store(self):
        blobs = list(self.vectors)
        sets = self.whole()
        sets[2] = (blobs[2], [A])               # the store holds B for this set
        self.write_arena(sets)
        rows, timings = self.search()
        self.assertEqual(rows, self.from_store()[0])
        self.assertEqual(self.reported(timings), ('arena', 0, 'inconsistent'))

    def test_a_reader_that_raises_is_no_arena(self):
        self.write_arena(self.whole())
        for failure in (RuntimeError('boom'), OSError('gone'), KeyError('x')):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(vectorcache, 'opened', side_effect=failure):
                rows, timings = self.search()
                self.assertEqual((rows, self.reported(timings)), (self.from_store()[0], ('store', None, 'absent')))

    def test_an_arena_of_another_store_or_spec_is_no_arena(self):
        self.write_arena(self.whole())
        current = self.directory / arena_types.CURRENT
        value = json.loads(current.read_text())
        current.write_text(json.dumps(dict(value, store_incarnation='0' * 32)))
        self.assertEqual(self.reported(self.search()[1]), ('store', None, 'absent'))
        current.write_text(json.dumps(dict(value, format=arena_types.FORMAT + 1)))
        self.assertEqual(self.reported(self.search()[1]), ('store', None, 'absent'))

    # -- filters ------------------------------------------------------------------------
    def test_an_excluded_card_is_read_from_neither(self):
        self.write_arena(self.whole())
        blob = list(self.vectors)[0]
        rows, timings = self.search(excluded_cards=[[self.paths[blob], blob]])
        self.assertEqual(rows, self.from_store(excluded_cards=[[self.paths[blob], blob]])[0])
        self.assertNotIn(self.paths[blob], [path for path, _score in rows])
        self.assertEqual((timings['vectorRows'], timings['vectorExcluded']), (2, 1))
        self.assertEqual(self.reported(timings), ('arena', 0, 'ok'))


class Heads(VectorCase):
    def test_the_batched_coverage_read_answers_like_one_lookup_per_blob(self):
        store, _, handle, manifest, recipe, profile = self.configured(5)
        for member in manifest['members']:
            self.put_vectors(store, member['blob_sha'], recipe, profile, [A])
        self.publish(store, handle, manifest, recipe, profile)
        cards = [{'blob_sha': member['blob_sha'], 'eligible': 1} for member in manifest['members']]
        cards += [dict(cards[0]), {'blob_sha': 'e' * 64, 'eligible': 0}]
        spec = {'recipe_id': recipe, 'profile_id': profile}
        with store._session() as db, mock.patch.object(view_accel_search, '_BLOB_BATCH', 2):
            statements = []
            db.set_trace_callback(statements.append)
            batched = view_accel_search.heads(store, db, cards, spec, None)
            db.set_trace_callback(None)
            single = store._rows_coverage(db, cards, spec, full=False)
            self.assertEqual(batched, single)
            # Five blobs in batches of two: three batches, two reads each.
            self.assertEqual(sum('FROM chunk_sets' in text or 'FROM embedding_sets' in text for text in statements), 6)
            missing = cards + [{'blob_sha': 'd' * 64, 'eligible': 1}]
            self.assertEqual(self.refused(view_accel_search.heads, store, db, missing, spec, None).reason,
                             self.refused(store._rows_coverage, db, missing, spec, full=False).reason)

    def test_a_set_whose_chunk_and_embedding_counts_differ_is_refused(self):
        store, _, handle, manifest, recipe, profile = self.configured(2)
        for member in manifest['members']:
            self.put_vectors(store, member['blob_sha'], recipe, profile, [A])
        self.publish(store, handle, manifest, recipe, profile)
        cards = [{'blob_sha': member['blob_sha'], 'eligible': 1} for member in manifest['members']]
        spec = {'recipe_id': recipe, 'profile_id': profile}
        with store._session() as db:
            real = db.execute

            def skewed(sql, *args):
                rows = list(real(sql, *args))
                if 'FROM embedding_sets' in sql:
                    rows = [(row[0], row[1] + 1, row[2]) for row in rows]
                return rows
            with mock.patch.object(view_accel_search.vectors, '_left'):
                proxy = mock.Mock(execute=skewed)
                self.assertEqual(self.refused(view_accel_search.heads, store, proxy, cards, spec, None).reason,
                                 'embedding_set_invalid')


class PythonLane(VectorCase):
    def test_query_vectors_the_work_budget_does_not_pay_for_are_dropped_from_the_end(self):
        store, _, handle, manifest, recipe, profile = self.configured(2)
        first, second = [member['blob_sha'] for member in manifest['members']]
        self.put_vectors(store, first, recipe, profile, [A])
        self.put_vectors(store, second, recipe, profile, [B])
        _, pin = self.publish(store, handle, manifest, recipe, profile)
        self.addCleanup(store.release, pin)
        paths = {member['blob_sha']: member['path'] for member in manifest['members']}
        work = 2 * 2  # Two chunks of dimension 2.

        def scores(budget):
            rows = store.search_exact(pin, [A, B], 2, profile_id=profile, max_float_work=budget)
            return {row['path']: round(row['score'], 6) for row in rows}
        # Paid for both vectors: each card meets its own vector.
        self.assertEqual(scores(2 * work), {paths[first]: 1.0, paths[second]: 1.0})
        # Paid for one: only the first vector is scored, and the search still answers.
        self.assertEqual(scores(2 * work - 1), {paths[first]: 1.0, paths[second]: 0.0})
        self.assertEqual(scores(work), {paths[first]: 1.0, paths[second]: 0.0})
        self.assertEqual(self.refused(store.search_exact, pin, [A, B], 2, profile_id=profile,
                                      max_float_work=work - 1).reason, 'vector_work_exceeded')
