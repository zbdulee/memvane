"""1.0.22 A-12: the exact-search phases outside the vector kernel carry timing fields.

Fields are added only to the caller's ``timings`` dict; the answer is the same
with or without it.
"""
import math
from unittest import mock

from memvane.store.tests import support

PHASES = ('vectorObserveMs', 'vectorOpenMs', 'vectorCardsMs', 'vectorSnapshotMs', 'vectorBodyMs',
          'vectorTotalMs')


class SearchTimings(support.VectorCase):
    def ready(self, count=12):
        store, _repo, h, manifest, r, p = self.configured(count)
        for i, member in enumerate(manifest['members']):
            self.put_vectors(store, member['blob_sha'], r, p, [[1., (i + 1) / count]])
        gen, pin = self.publish(store, h, manifest, r, p)
        self.addCleanup(store.release, pin)
        return store, p, pin

    def test_t12_1_phase_fields_are_added_and_results_do_not_change(self):
        store, p, pin = self.ready()
        for accel in ('on', 'off'):
            with self.subTest(accel=accel), mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': accel}):
                plain = store.search_exact(pin, [1., 0.], 5, profile_id=p)
                stats = {}
                timed = store.search_exact(pin, [1., 0.], 5, profile_id=p, timings=stats)
                self.assertEqual(timed, plain)
                for field in PHASES:
                    self.assertTrue(math.isfinite(stats[field]) and stats[field] >= 0, field)
                self.assertGreaterEqual(stats['vectorTotalMs'] + 1e-6,
                                        max(stats[field] for field in PHASES[:-1]))

    def test_t12_1_a_deadline_keeps_the_phases_it_reached(self):
        store, p, pin = self.ready()
        stats = {}
        from memvane.store.view import view_vectors
        with mock.patch.object(view_vectors, '_python_top', side_effect=ValueError('vector_deadline_exceeded')), \
                mock.patch.dict('os.environ', {'MEMVANE_VECTOR_ACCEL': 'off'}):
            with self.assertRaises(Exception):
                store.search_exact(pin, [1., 0.], 5, profile_id=p, timings=stats)
        self.assertIn('vectorOpenMs', stats)
        self.assertIn('vectorCardsMs', stats)
        self.assertIn('vectorTotalMs', stats)
        self.assertNotIn('vectorBodyMs', stats)
