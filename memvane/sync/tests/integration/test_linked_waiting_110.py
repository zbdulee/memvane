"""A pass that published the main view is a success whatever its linked views wait for.

The linked views a pass left without a current generation are counted by
reason in ``linkedWaiting``. The same wake is offered again only by a pass
that moved something; a pass without progress ends and the next sync request
reaches the linked views. Only a main view that did not publish fails the pass.
"""
from contextlib import contextmanager
import unittest
from unittest import mock

from memvane.store.types import BlobStoreError
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase

LINKED_VIEWS = 12
PER_PASS = 4


class LinkedWaiting(SyncCase):
    def setUp(self):
        super().setUp()
        self.linked = []
        for index in range(LINKED_VIEWS):
            linked = self.fixture.linked_worktree('w%02d' % index, 'side%02d' % index)
            self.card('concepts/side%02d.md' % index, 'wren%02dprobe' % index, project=linked)
            linked.commit('side')
            self.linked.append(linked)

    def wakes(self):
        wakes = []
        return wakes, mock.patch('memvane.runtime.api.request',
                                 lambda kind, target='', **kw: wakes.append((kind, target, kw.get('delay', 0)))
                                 or {'status': 'queued'})

    @contextmanager
    def budget_for(self, views):
        """The pass budget ends once ``views`` linked views did work (a count, not a clock).

        A view that is already current costs nothing, as a cached capture does.
        """
        real, seen = worker._publish_schema3_view, []

        def counted(*args, **kwargs):
            result = real(*args, **kwargs)
            if not kwargs.get('window') and result['state'] != 'unchanged':
                seen.append(1)
                if len(seen) >= views:
                    worker.SCHEMA3_PUBLISH_RESERVE = 10 ** 6
            return result
        with mock.patch.object(worker, 'SCHEMA3_PUBLISH_RESERVE', worker.SCHEMA3_PUBLISH_RESERVE), \
                mock.patch.object(worker, '_publish_schema3_view', counted):
            yield

    def states(self, result):
        return [item['state'] for item in result.get('worktrees') or []]

    def test_a_pass_that_reaches_some_linked_views_succeeds_and_offers_the_wake_again(self):
        wakes, recorded = self.wakes()
        left = LINKED_VIEWS
        while left > 0:
            del wakes[:]
            with recorded, self.budget_for(PER_PASS):
                result = self.run_pass()
            left -= PER_PASS
            self.assertIn(result['state'], ('indexed', 'unchanged'), result)
            self.assertNotIn('reason', result, result)
            self.assertEqual(self.states(result).count('pending'), max(left, 0), result)
            if left > 0:
                self.assertEqual(result['linkedWaiting'],
                                 {'waiting': left, 'held': 0, 'reasons': {'schema3_pass_budget': left}}, result)
                self.assertEqual(wakes, [('laya-sync', str(self.project), worker.SCHEMA3_VECTOR_REOFFER_SECONDS)])
        self.assertNotIn('linkedWaiting', result)
        del wakes[:]
        with recorded:
            settled = self.run_pass()
        self.assertEqual((settled['state'], self.states(settled)),
                         ('unchanged', ['unchanged'] * LINKED_VIEWS), settled)
        self.assertNotIn('linkedWaiting', settled)
        self.assertEqual(wakes, [])

    def test_a_pass_without_progress_succeeds_and_does_not_offer_the_wake_again(self):
        roots = {str(linked.root) for linked in self.linked}

        def busy(real, worktree_id, path, *, decision, **kwargs):
            if decision['projectRoot'] in roots:
                raise BlobStoreError('candidate_busy')
            return real(worktree_id, path, decision=decision, **kwargs)
        wakes, recorded = self.wakes()
        with recorded, self.store_methods(ingest_attested_card=busy):
            result = self.run_pass()
        self.assertEqual(result['state'], 'unchanged', result)
        self.assertNotIn('reason', result, result)
        self.assertEqual(result['linkedWaiting']['waiting'], LINKED_VIEWS, result)
        self.assertEqual(sum(result['linkedWaiting']['reasons'].values()), LINKED_VIEWS, result)
        self.assertNotIn('linkedReoffer', result)
        self.assertEqual(wakes, [])

    def test_a_main_view_that_did_not_publish_fails_the_pass(self):
        self.card(self.CARDS[0][0], 'falconprobe')
        main = str(self.project)

        def busy(real, worktree_id, path, *, decision, **kwargs):
            if decision['projectRoot'] == main:
                raise BlobStoreError('candidate_busy')
            return real(worktree_id, path, decision=decision, **kwargs)
        wakes, recorded = self.wakes()
        with recorded, self.store_methods(ingest_attested_card=busy):
            result = self.run_pass()
        self.assertEqual((result['state'], result['reason']), ('deferred', 'candidate_busy'), result)
        self.assertNotIn('linkedWaiting', result)
        self.assertNotIn('worktrees', result)
        self.assertEqual(wakes, [])


class Summary(unittest.TestCase):
    def test_views_are_counted_by_what_they_wait_for(self):
        linked = [
            {'state': 'unchanged'},
            {'state': 'held', 'reason': 'worktree_missing'},
            {'state': 'pending', 'reason': 'schema3_pass_budget'},
            {'state': 'deferred', 'reason': 'candidate_busy'},
            {'state': 'unchanged', 'vector': {'state': 'pending', 'reason': 'schema3_vector_budget', 'progress': 0}},
            {'state': 'pending'},
        ]
        summary, moved = worker._linked_waiting(linked)
        self.assertEqual(summary, {'waiting': 4, 'held': 1, 'reasons': {
            'worktree_missing': 1, 'schema3_pass_budget': 1, 'candidate_busy': 1,
            'schema3_vector_budget': 1, 'unlabeled': 1}})
        self.assertFalse(moved)

    def test_current_views_have_no_summary(self):
        self.assertEqual(worker._linked_waiting([{'state': 'unchanged'}, {'state': 'indexed'}]), (None, True))
        self.assertEqual(worker._linked_waiting([]), (None, False))

    def test_a_view_that_ingested_or_filled_vectors_moved(self):
        for item in ({'state': 'indexed'}, {'state': 'pending', 'reason': 'schema3_view_preparing', 'ingested': 3},
                     {'state': 'unchanged', 'vector': {'state': 'pending', 'progress': 2}},
                     {'state': 'unchanged', 'vector': {'state': 'pending', 'filled': 1}}):
            with self.subTest(item=item):
                self.assertTrue(worker._linked_waiting([item])[1])
        for item in ({'state': 'unchanged'}, {'state': 'pending', 'reason': 'schema3_pass_budget'},
                     {'state': 'pending', 'ingested': 0},
                     {'state': 'unchanged', 'vector': {'state': 'pending', 'progress': 0}},
                     {'state': 'unchanged', 'vector': {'state': 'complete', 'filled': 4}}):
            with self.subTest(item=item):
                self.assertFalse(worker._linked_waiting([item])[1])
