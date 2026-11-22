"""A pass that published the main view is a success whatever its linked views wait for.

The linked views a pass left without a current generation are counted by
reason in ``linkedWaiting``. The same wake is offered again only by a pass
that moved something; a pass without progress ends and the next sync request
reaches the linked views. Only a main view that did not publish fails the pass.
"""
from contextlib import contextmanager
import json
import unittest
from unittest import mock

from memvane.store.types import BlobStoreError
from memvane.sync import linked_reoffer
from memvane.sync import product_sync_worker as worker
from memvane.sync.tests.support import SyncCase
from tests.support.case import FixtureCase

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
        self.assertNotIn('vectorReoffer', result)
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
            {'root': '/a', 'state': 'unchanged'},
            {'root': '/b', 'state': 'held', 'reason': 'worktree_missing'},
            {'root': '/c', 'state': 'pending', 'reason': 'schema3_pass_budget'},
            {'root': '/d', 'state': 'deferred', 'reason': 'candidate_busy'},
            {'root': '/e', 'state': 'unchanged',
             'vector': {'state': 'pending', 'reason': 'schema3_vector_budget', 'progress': 0}},
            {'root': '/f', 'state': 'pending', 'ingested': 3, 'remaining': 40},
        ]
        summary, backlog = worker._linked_waiting(linked)
        self.assertEqual(summary, {'waiting': 4, 'held': 1, 'reasons': {
            'worktree_missing': 1, 'schema3_pass_budget': 1, 'candidate_busy': 1,
            'schema3_vector_budget': 1, 'unlabeled': 1}})
        # A held view is not waiting; only a counted ``remaining`` is carried.
        self.assertEqual(backlog, {'views': 4, 'unreached': 1, 'remaining': {'/f': 40}})
        # A published view still filling vectors carries its targets left.
        _summary, filling = worker._linked_waiting([{'root': '/g', 'state': 'unchanged', 'vector': {
            'state': 'pending', 'reason': 'schema3_vector_deadline', 'progress': 3, 'remaining': 17}}])
        self.assertEqual(filling, {'views': 1, 'unreached': 0, 'remaining': {'/g#vector': 17}})

    def test_current_and_held_views_have_no_backlog(self):
        self.assertEqual(worker._linked_waiting([{'root': '/a', 'state': 'unchanged'},
                                                 {'root': '/b', 'state': 'indexed'}]), (None, None))
        self.assertEqual(worker._linked_waiting([]), (None, None))
        summary, backlog = worker._linked_waiting([{'root': '/a', 'state': 'held', 'reason': 'worktree_missing'}])
        self.assertEqual((summary['held'], backlog), (1, None))


class Reoffer(FixtureCase):
    """``linked_reoffer.decide``: a re-offer needs a waiting view with less left, and has a cap."""

    def setUp(self):
        super().setUp()
        self.state = str(self.tmp)
        self.path = self.tmp / linked_reoffer.NAME

    def decide(self, views=1, unreached=0, **remaining):
        return linked_reoffer.decide(self.state, {'views': views, 'unreached': unreached,
                                                  'remaining': {'/' + k: v for k, v in remaining.items()}})

    def test_a_view_the_pass_did_not_reach_or_with_less_left_is_offered_again(self):
        self.assertEqual(self.decide(views=8, unreached=8), 'offer')
        self.assertEqual(self.decide(views=4, unreached=4), 'offer')
        # First sight of a counted view, then less left, then the same amount.
        self.assertEqual(self.decide(a=136), 'offer')
        self.assertEqual(self.decide(a=72), 'offer')
        self.assertEqual(self.decide(a=72), 'none')
        self.assertEqual(self.decide(a=72), 'none')
        self.assertEqual(self.decide(a=8), 'offer')

    def test_a_view_that_waits_for_another_reason_is_not_offered_whatever_else_moved(self):
        # No counted remaining and not a budget wait: the service-engineering shape.
        for _ in range(3):
            self.assertEqual(self.decide(views=2), 'none')
        # Work that leaves as much as before is no reason either.
        self.assertEqual(self.decide(views=2, a=64, b=64), 'offer')
        self.assertEqual(self.decide(views=2, a=64, b=64), 'none')
        self.assertEqual(self.decide(views=2, a=64, b=70), 'none')

    def test_offers_without_a_new_low_are_capped_and_a_new_low_starts_over(self):
        self.assertEqual(self.decide(views=3, unreached=3), 'offer')
        answers = [self.decide(views=3, unreached=3) for _ in range(linked_reoffer.CAP + 3)]
        self.assertEqual(answers, ['offer'] * linked_reoffer.CAP + ['capped'] * 3)
        self.assertEqual(self.decide(views=2, unreached=2), 'offer')
        self.assertEqual(json.loads(self.path.read_text())['streak'], 0)

    def test_views_counted_in_turn_do_not_hide_that_each_only_shrinks(self):
        # Two cold views; the pass budget reaches one of them per pass, the other is not counted.
        self.assertEqual(self.decide(views=2, unreached=1, a=900), 'offer')
        left = {'a': 900, 'b': 900}
        answers = []
        for index in range(3 * linked_reoffer.CAP):
            name = 'ab'[index % 2]
            left[name] -= 10
            answers.append(self.decide(views=2, unreached=1, **{name: left[name]}))
        self.assertEqual(set(answers), {'offer'})
        self.assertEqual(json.loads(self.path.read_text())['streak'], 0)
        # The same turn-taking with nothing getting smaller is capped.
        answers = [self.decide(views=2, unreached=1, **{'ab'[index % 2]: left['ab'[index % 2]]})
                   for index in range(linked_reoffer.CAP + 2)]
        self.assertEqual(answers[-1], 'capped')

    def test_new_work_beside_a_view_that_keeps_waiting_is_never_capped_while_it_shrinks(self):
        # One view waits for a reason no pass clears; another worktree gets 200 cards again and again.
        answers = []
        for _job in range(30):
            for left in (136, 72, 8):
                answers.append(self.decide(views=2, b=left))
            answers.append(self.decide(views=1))  # The job is done; the other view still waits.
        self.assertEqual(answers, ['offer', 'offer', 'offer', 'none'] * 30)
        self.assertLessEqual(json.loads(self.path.read_text())['streak'], 1)
        # The same view asked again and again with as much left is still cut off.
        self.decide(views=2, b=136)
        stuck = [self.decide(views=2, unreached=1, b=136) for _ in range(linked_reoffer.CAP + 2)]
        self.assertEqual(stuck[-1], 'capped')

    def test_a_count_that_rises_is_no_reason_and_only_moves_the_low(self):
        self.assertEqual(self.decide(a=10), 'offer')
        self.assertEqual(self.decide(a=30), 'none')
        self.assertEqual(json.loads(self.path.read_text())['floors'], {'/a': 30})
        self.assertEqual(self.decide(a=20), 'offer')
        self.assertEqual(json.loads(self.path.read_text())['streak'], 0)

    def test_more_views_than_are_followed_do_not_read_as_new_ones(self):
        # Seventy views wait; each pass counts a different half of them and none gets smaller.
        names = ['v%02d' % index for index in range(70)]
        answers = []
        for index in range(linked_reoffer.CAP + 6):
            counted = names[index % 2::2]
            answers.append(self.decide(views=70, unreached=35, **{name: 50 for name in counted}))
        self.assertEqual(answers[-1], 'capped')
        saved = json.loads(self.path.read_text())
        self.assertLessEqual(len(saved['remaining']), 64)
        self.assertLessEqual(len(saved['floors']), 256)

    def test_a_linked_views_vector_targets_left_count_like_its_cards(self):
        self.assertEqual(self.decide(**{'a#vector': 40}), 'offer')
        self.assertEqual(self.decide(**{'a#vector': 31}), 'offer')
        self.assertEqual(self.decide(**{'a#vector': 31}), 'none')

    def test_a_pass_with_nothing_waiting_forgets_the_history(self):
        for _ in range(linked_reoffer.CAP + 2):
            self.decide(views=3, unreached=3)
        self.assertEqual(linked_reoffer.decide(self.state, None), 'none')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.decide(views=3, unreached=3), 'offer')

    def test_an_unreadable_history_reads_as_none_and_an_unwritable_one_offers_nothing(self):
        self.path.write_text('{"streak": "many", "remaining": [1], "floor"')
        self.assertEqual(self.decide(a=10), 'offer')
        self.assertEqual(json.loads(self.path.read_text()),
                         {'floors': {'/a': 10}, 'remaining': {'/a': 10}, 'streak': 0, 'views': 1})
        self.assertEqual(linked_reoffer.decide(None, {'views': 1, 'unreached': 1, 'remaining': {}}), 'none')
        with mock.patch('memvane.kernel.atomic_rename.atomic_json', side_effect=OSError('read-only')):
            self.assertEqual(self.decide(views=5, unreached=5), 'none')


    def test_a_damaged_history_is_replaced_and_the_cap_still_ends_the_offers(self):
        self.path.write_text('not json')
        answers = [self.decide(views=3, unreached=3) for _ in range(linked_reoffer.CAP + 2)]
        self.assertEqual(answers, ['offer'] * (linked_reoffer.CAP + 1) + ['capped'])
        # Only the views that wait now are kept, and no partial file is left beside it.
        self.decide(views=2, a=5, b=6)
        self.decide(views=1, b=6)
        self.assertEqual(json.loads(self.path.read_text())['remaining'], {'/b': 6})
        self.assertEqual(sorted(item.name for item in self.tmp.iterdir()), [linked_reoffer.NAME])


class SettleVectors(unittest.TestCase):
    """The tail of ``_run_sync``: which pass results stay a success."""

    def settle(self, result, linked='none', fails=False):
        calls = []

        def reoffer():
            calls.append(1)
            if fails:
                raise OSError('queue')
        settled = worker._settle_vectors(result, reoffer, linked)
        return settled['state'], settled.get('reason'), settled.get('vectorReoffer', False), len(calls)

    def test_only_the_main_views_vectors_a_provider_defect_or_a_capped_wait_fail_the_pass(self):
        moved = {'state': 'pending', 'reason': 'schema3_vector_deadline', 'progress': 3}
        stuck = {'state': 'pending', 'reason': 'schema3_vector_deadline', 'progress': 0}
        resource = {'state': 'deferred', 'reason': 'memory_reserve'}
        defect = {'state': 'provider_blocked', 'reason': 'provider_identity_changed'}
        done = {'state': 'published'}

        def result(main=None, *linked, state='unchanged'):
            return {'state': state, **({'vector': main} if main else {}),
                    **({'worktrees': [{'root': '/w%d' % i, 'state': 'unchanged', **({'vector': v} if v else {})}
                                      for i, v in enumerate(linked)]} if linked else {})}
        cases = (
            # result, linked decision, re-request fails -> state, reason, re-offered, requests
            (result(done), 'none', False, ('unchanged', None, False, 0)),
            (result(moved), 'none', False, ('unchanged', None, True, 1)),
            (result(stuck), 'none', False, ('pending', 'schema3_vector_deadline', False, 0)),
            (result(resource), 'none', False, ('deferred', 'memory_reserve', False, 0)),
            # The main view's stuck vectors fail the pass even while a linked view is offered again.
            (result(stuck, None), 'offer', False, ('pending', 'schema3_vector_deadline', False, 0)),
            (result(resource, moved), 'offer', False, ('deferred', 'memory_reserve', False, 0)),
            # Only a linked view's vectors wait: a success, offered again when that view has less left.
            (result(done, stuck), 'none', False, ('unchanged', None, False, 0)),
            (result(done, moved), 'offer', False, ('unchanged', None, True, 1)),
            (result(None, resource, state='indexed'), 'offer', False, ('indexed', None, True, 1)),
            # A provider defect met by a linked view is the install's.
            (result(done, defect), 'offer', False, ('blocked', 'provider_identity_changed', False, 0)),
            (result(moved, defect), 'none', False, ('blocked', 'provider_identity_changed', False, 0)),
            # A re-request that fails: a linked wait stays a success, the main view's vectors keep the wake.
            (result(done, moved), 'offer', True, ('unchanged', None, False, 1)),
            (result(moved), 'none', True, ('pending', 'schema3_vector_deadline', False, 1)),
            # Too many offers without less to do: a counted failure.
            (result(done, stuck), 'capped', False, ('pending', 'schema3_linked_pending', False, 0)),
            (result(moved, stuck), 'capped', False, ('unchanged', None, True, 1)),
        )
        for index, (value, linked, fails, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(self.settle(value, linked, fails), expected)

    def test_an_arena_that_is_catching_up_offers_the_wake_and_defers_a_capped_linked_wait(self):
        calls = []
        result = {'state': 'unchanged', 'vector': {'state': 'current'},
                  'worktrees': [{'root': '/w0', 'state': 'pending', 'reason': 'schema3_pass_budget'}]}
        # While the arena's catch-up moves, the pass is acknowledged and offered again: the capped
        # wait is not recorded as a failure in that pass.
        settled = worker._settle_vectors(result, lambda: calls.append(1), 'capped', True)
        self.assertEqual((settled['state'], settled.get('reason'), settled.get('vectorReoffer'), len(calls)),
                         ('unchanged', None, True, 1))
        # Once it has caught up the capped wait fails the pass as before.
        settled = worker._settle_vectors(result, lambda: calls.append(1), 'capped', False)
        self.assertEqual((settled['state'], settled['reason'], settled.get('vectorReoffer'), len(calls)),
                         ('pending', 'schema3_linked_pending', None, 1))

    def test_a_pass_that_did_not_publish_is_left_as_it_is(self):
        for linked in ('none', 'offer', 'capped'):
            self.assertEqual(self.settle({'state': 'deferred', 'reason': 'candidate_busy'}, linked),
                             ('deferred', 'candidate_busy', False, 0))
