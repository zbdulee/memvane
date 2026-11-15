"""Recall's served answer: selection, trace reports and the plain answer the host renders.

Rows are synthetic; learning, route and journal are replaced at their api
boundaries, so no store, model or install state is opened.
"""
import contextlib
import io
import unittest
from unittest import mock

from memvane.recall import recall_scope, verified_rows
from memvane.recall.types import HostResponseError, RecallAnswer, RecallTrace


class Recorded(RecallTrace):
    def __init__(self):
        self.events = []

    def fields(self, **fields):
        self.events.append(('fields', fields))

    def note(self, reason):
        self.events.append(('note', reason))

    def final(self, reason):
        self.events.append(('final', reason))


DECISION = {'projectRoot': '/synthetic/project', 'scopeId': 'a' * 32, 'installId': 'install',
            'journalPath': '/synthetic/state/journal.sqlite',
            'collections': [{'name': 'wiki', 'role': 'wiki', 'sourceRoot': '/synthetic/project/wiki'}]}


def rows(count):
    return [{'path': 'wiki/c%d.md' % index} for index in range(count)]


class ServeVerifiedRows(unittest.TestCase):
    def serve(self, selected, *, count=6, top_n=5, current=True, title_only=frozenset(),
              locations=None, vector_fallback=None):
        trace, checked = Recorded(), []

        def still_current(chosen):
            checked.append(list(chosen))
            return current
        found = rows(count)
        with mock.patch.object(verified_rows, '_select_verified_cards',
                               return_value=selected(found)):
            answer = verified_rows._serve_verified_rows(
                DECISION, {'topN': top_n}, 'find', found, top_n=top_n, title_only=title_only,
                locations=dict(locations or {}), generation='gen-1',
                still_current=still_current, vector_fallback=vector_fallback,
                allow_mixed_generation=False, trace=trace, own_view=True)
        return answer, trace.events, checked, found

    def test_selected_cards_are_rechecked_and_returned_with_their_metadata(self):
        answer, events, checked, found = self.serve(
            lambda found: (found[:4], 'selected', None),
            title_only=frozenset({'wiki/c1.md', 'wiki/c5.md'}),
            locations={'wiki/c2.md': ('docs/a.md#L1-L2',), 'wiki/c4.md': ('docs/b.md#L3-L3',)},
            vector_fallback='selected_view_vectors_pending')
        self.assertIsInstance(answer, RecallAnswer)
        self.assertEqual(answer.rows, found[:3])
        self.assertEqual(checked, [found[:3]])
        self.assertEqual(answer.limit, 3)
        self.assertEqual(answer.ranking, 'laya_selection')
        self.assertEqual(answer.title_only_paths, frozenset({'wiki/c1.md'}))
        self.assertEqual(answer.source_locations, {'wiki/c2.md': ('docs/a.md#L1-L2',)})
        self.assertEqual((answer.selection_fallback, answer.vector_fallback),
                         (None, 'selected_view_vectors_pending'))
        self.assertEqual(answer.capture, ({'topN': 5}, found, found[:3]))
        self.assertIs(answer.decision, DECISION)
        self.assertTrue(answer.own_view)
        self.assertEqual(events, [('fields', {'selection': 'selected', 'candidates': 6,
                                              'generation': 'gen-1'})])

    def test_ranked_rows_keep_hybrid_rank_and_name_their_fallback(self):
        answer, _events, _checked, found = self.serve(
            lambda found: (found, 'ranked', 'laya_selector_not_prepared'), top_n=2)
        self.assertEqual((answer.rows, answer.limit, answer.ranking),
                         (found[:2], 2, 'hybrid_rrf'))
        self.assertEqual(answer.selection_fallback, 'laya_selector_not_prepared')

    def test_a_zero_card_choice_answers_empty_with_its_note(self):
        answer, events, checked, found = self.serve(lambda found: ([], 'selected', None))
        self.assertEqual((answer.rows, checked), ((), []))
        self.assertEqual(answer.capture, ({'topN': 5}, found, []))
        self.assertEqual(events[1:], [('note', 'laya_selector_selected_zero')])

    def test_no_candidates_is_the_final_reason(self):
        answer, events, _checked, _found = self.serve(
            lambda found: ([], 'no_candidates', None), count=0)
        self.assertEqual(answer.rows, ())
        self.assertEqual(events, [('fields', {'selection': 'no_candidates', 'candidates': 0,
                                              'generation': 'gen-1'}),
                                  ('final', 'no_candidates')])

    def test_a_moved_proof_refuses_after_selection(self):
        with self.assertRaisesRegex(HostResponseError, '^selected_proof_changed$'):
            self.serve(lambda found: (found[:1], 'selected', None), current=False)


class SelectVerifiedCards(unittest.TestCase):
    def select(self, *, prepared, selected=None, published=None):
        trace = Recorded()
        journal = {'state': 'selected', 'installId': 'install', 'maxPluginVersion': '0.0.1'}
        stderr = io.StringIO()
        with mock.patch('memvane.route.api.wiki_index_roots', return_value={'wiki': '/w'}), \
                mock.patch('memvane.journal.api.inspect_journal', return_value=journal), \
                mock.patch('memvane.learning.api.selector_prepared', return_value=prepared), \
                mock.patch('memvane.learning.api.select_published',
                           side_effect=selected or AssertionError('selector called')), \
                mock.patch('memvane.learning.api.published_candidate_rows',
                           side_effect=published), \
                mock.patch('memvane.resident.api.ensure_service', None), \
                contextlib.redirect_stderr(stderr):
            result = verified_rows._select_verified_cards(DECISION, {}, 'find', rows(20), trace)
        return result, trace.events, stderr.getvalue()

    def test_unprepared_selector_serves_the_verified_pool_by_rank(self):
        (chosen, reason, fallback), events, _err = self.select(prepared=False)
        self.assertEqual((chosen, reason, fallback),
                         (rows(15), 'ranked', 'laya_selector_not_prepared'))
        self.assertEqual(events, [])

    def test_selector_answer_is_final(self):
        answer = {'reason': 'selected', 'final_results': rows(2)}
        (chosen, reason, fallback), events, _err = self.select(
            prepared=True, selected=lambda *_a, **_k: answer)
        self.assertEqual((chosen, reason, fallback), (rows(2), 'selected', None))
        self.assertEqual(events, [])

    def test_unavailable_selector_falls_back_with_a_note(self):
        answer = {'reason': 'candidate_unavailable', 'final_results': []}
        (chosen, reason, fallback), events, err = self.select(
            prepared=True, selected=lambda *_a, **_k: answer)
        self.assertEqual((chosen, reason, fallback),
                         (rows(15), 'ranked', 'laya_selector_candidate_unavailable'))
        self.assertEqual(events, [('note', 'laya_selector_candidate_unavailable')])
        self.assertIn('serving hybrid rank', err)

    def test_invalid_pool_serves_nothing(self):
        answer = {'reason': 'candidate_invalid', 'final_results': []}
        (chosen, reason, _fallback), events, _err = self.select(
            prepared=True, selected=lambda *_a, **_k: answer)
        self.assertEqual((chosen, reason), ([], 'candidate_invalid'))
        self.assertEqual(events, [('note', 'laya_selector_candidate_invalid')])
        (chosen, reason, _fallback), events, _err = self.select(
            prepared=False, published=ValueError('invalid'))
        self.assertEqual((chosen, reason), ([], 'candidate_invalid'))
        self.assertEqual(events, [('note', 'laya_selector_candidate_invalid')])


class RecallScope(unittest.TestCase):
    def test_epoch_floor_and_one_wake_per_root_stay_inside_their_scope(self):
        calls = []

        def request(kind, target='', **kwargs):
            calls.append((kind, target, kwargs))
            return {'status': 'queued'}
        with mock.patch('memvane.runtime.api.request', side_effect=request):
            with recall_scope.recall_scope():
                recall_scope._recall_epoch({'selectionEpoch': 4})
                with self.assertRaisesRegex(HostResponseError, '^selected_proof_rolled_back$'):
                    recall_scope._recall_epoch({'selectionEpoch': 3})
                recall_scope._recall_sync_wake('/p')
                recall_scope._recall_sync_wake('/p')
                self.assertTrue(recall_scope._recall_open())
                recall_scope.set_writer_allowed(False)
                self.assertIsNone(recall_scope._recall_sync_wake('/q'))
            self.assertFalse(recall_scope._recall_open())
            recall_scope._recall_epoch({'selectionEpoch': 1})
            recall_scope._recall_sync_wake('/p')
        self.assertEqual(calls, [('laya-sync', '/p', {}), ('laya-sync', '/p', {})])

    def test_proof_may_only_advance_its_epoch(self):
        proof = {'a': 1, 'selectionEpoch': 2}
        self.assertTrue(recall_scope._proof_still_current(dict(proof), proof))
        self.assertTrue(recall_scope._proof_still_current({'a': 1, 'selectionEpoch': 3}, proof))
        self.assertFalse(recall_scope._proof_still_current({'a': 1, 'selectionEpoch': 1}, proof))
        self.assertFalse(recall_scope._proof_still_current({'a': 2, 'selectionEpoch': 2}, proof))


if __name__ == '__main__':
    unittest.main()
