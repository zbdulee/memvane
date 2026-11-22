"""A long prompt is asked as up to four windows in one embedding child.

The recalls run ``RecallInstallCase`` with an admitted fixture provider that
logs the length of every query text it is asked. The window rule itself is
checked in the provider domain (``test_query_chunks``).
"""
from __future__ import annotations

from unittest import mock

from memvane.recall import reader_dispatch
from memvane.recall.tests.support import RecallInstallCase

PROMPT = 'what marker does the heron protocol keep?'


def long_prompt(chars):
    filler = ' The osprey ledger adds nothing to this question.'
    text = PROMPT + filler * (chars // len(filler) + 1)
    return text[:chars - len(PROMPT) - 1] + ' ' + PROMPT


class LongPromptRecall(RecallInstallCase):
    CARDS = 2

    def setUp(self):
        super().setUp()
        self.provider = self.install.provider
        self.provider.admit_queries()

    def recall_counted(self, prompt):
        """``(context, event, embedding children, provider wakes)`` of one recall."""
        from memvane.provider.api import query_embed_child_argv
        real, child = reader_dispatch.subprocess.Popen, query_embed_child_argv()
        # ``subprocess`` is one module: the stand-in sees every child of the recall (git too).
        with mock.patch.object(reader_dispatch.subprocess, 'Popen', side_effect=real) as popen, \
                mock.patch('memvane.recall.recall_scope._wake_provider') as wake:
            context, _err, event = self.recall(prompt)
        children = sum(call.args[0] == child for call in popen.call_args_list)
        return context, event, children, wake.call_count

    def test_a_twelve_thousand_character_prompt_asks_four_windows_in_one_child(self):
        self.provider.bound_queries(1500)
        context, event, children, wakes = self.recall_counted(long_prompt(12000))
        self.assertEqual(self.provider.query_lengths(), [1200, 1200, 1200, 1200])
        self.assertEqual((children, wakes), (1, 0))
        self.assertEqual((event['semantic'], event['queryChunks'], event['queryChunksRequested']),
                         ('complete', 4, 4), event)
        self.assertIn('zqheronx', context)
        self.assertIn('semanticOnlyInjected', event)
        self.assertNotIn('vector_fallback', event)
        # One cosine per injected card, in injection order; the other candidates' paths and cosines stay off the row.
        self.assertEqual(len(event['injectedCosine']), len(event['cards']))
        self.assertTrue(event['cards'])
        self.assertTrue(all(value is None or -1.0 <= value <= 1.0001 for value in event['injectedCosine']))
        self.assertTrue(any(value is not None for value in event['injectedCosine']))
        self.assertNotIn('semanticCosine', event)

    def test_a_five_thousand_character_prompt_that_fails_whole_goes_on_as_windows(self):
        self.provider.bound_queries(1500)
        context, event, children, wakes = self.recall_counted(long_prompt(5000))
        self.assertEqual(self.provider.query_lengths(), [5000, 1200, 1200, 1200, 1200])
        self.assertEqual((children, wakes), (1, 0))
        self.assertEqual((event['semantic'], event['queryChunks'], event['queryChunksRequested']),
                         ('complete', 4, 5), event)
        self.assertIn('zqheronx', context)

    def test_a_prompt_answered_whole_is_served_as_before(self):
        self.provider.bound_queries(None)
        context, event, children, _wakes = self.recall_counted(PROMPT)
        self.assertEqual(self.provider.query_lengths(), [len(PROMPT)])
        self.assertEqual((children, event['semantic'], event['queryChunks'], event['queryChunksRequested']),
                         (1, 'complete', 1, 1), event)
        # The same recall with the windows of a long prompt serves the same cards.
        long_context, long_event, _children, _wakes = self.recall_counted(long_prompt(12000))
        self.assertEqual([card['path'] for card in long_event['cards']], [card['path'] for card in event['cards']])
        self.assertIn('zqheronx', long_context)
        self.assertIn('zqheronx', context)

    def test_a_long_prompt_whose_every_window_fails_is_served_lexically(self):
        self.provider.bound_queries(10)
        context, event, children, wakes = self.recall_counted(long_prompt(12000))
        self.assertEqual(len(self.provider.query_lengths()), 8)
        self.assertEqual((children, event['semantic']), (1, 'provider_absent'), event)
        self.assertNotIn('queryChunks', event)
        self.assertEqual(wakes, 1)
        self.assertIn('zqheronx', context)


class EditQuery(RecallInstallCase):
    """An edit recall asks its query whole, whatever its length: its pool is a learning case."""

    CARDS = 2

    def edit_recall(self, prompt):
        import time
        from memvane.journal import api as journal_api
        from memvane.recall import recall_scope, selected_recall
        from memvane.recall.types import RecallTrace
        from tests.support.install_builder import ROOT

        class Trace(RecallTrace):
            def __init__(self):
                self.data = {}

            def fields(self, **fields):
                self.data.update(fields)
        decision = journal_api.select_route(str(self.project), action='recall')
        with journal_api.resolve_endpoint(self.project, action='recall', host_root=ROOT) as endpoint:
            _record, _attestation, proof = journal_api.recheck_reader(endpoint)
        trace = Trace()
        with recall_scope.recall_scope():
            recall_scope.set_edit_recall(deadline=time.monotonic() + 10, selector_deadline=time.monotonic() + 10)
            answer = selected_recall.serve_endpoint_recall(
                'claude', {'cwd': str(self.project), 'prompt': prompt, 'session_id': 'edit-whole'},
                cwd=str(self.project), trace=trace, route_decision=decision, main_proof=proof)
        return answer, trace.data

    def test_a_long_edit_query_is_one_whole_request(self):
        provider = self.install.provider
        provider.admit_queries()
        provider.bound_queries(None)
        query = long_prompt(8192)
        answer, data = self.edit_recall(query)
        self.assertEqual(provider.query_lengths(), [len(query)])
        self.assertEqual((data['semantic'], data['queryChunks'], data['queryChunksRequested']), ('complete', 1, 1))
        self.assertTrue(answer.rows)

    def test_a_long_edit_query_the_provider_refuses_is_not_asked_as_windows(self):
        provider = self.install.provider
        provider.admit_queries()
        provider.bound_queries(1500)
        query = long_prompt(6001)
        answer, data = self.edit_recall(query)
        self.assertEqual(provider.query_lengths(), [len(query)])
        self.assertEqual(data['semantic'], 'provider_absent')
        self.assertNotIn('queryChunks', data)
        self.assertTrue(answer.rows, 'the lexical lists still answer')


class ExcludedHits(RecallInstallCase):
    """What an empty recall counts as removed by the session exclusions."""

    class _Store:
        def __init__(self, rows):
            self.rows, self.asked = rows, []

        def search(self, pin, query, limit, **options):
            self.asked.append((query, options.get('excluded_cards')))
            return self.rows

    def test_the_exact_filter_count_is_added_only_when_the_semantic_search_completed(self):
        import time
        rows = [{'path': 'a.md', 'blob_sha': 'a' * 64}, {'path': 'b.md', 'blob_sha': 'b' * 64}]
        excluded = [['a.md', 'a' * 64], ['b.md', 'f' * 64]]
        deadline = time.monotonic() + 30
        for status, expected in (('complete', 1 + 5), ('deferred_deadline', 1), ('provider_absent', 1)):
            store = self._Store(rows)
            with self.subTest(status=status):
                hits = reader_dispatch._excluded_hits(store, None, ['q'], excluded, status,
                                                      {'vectorExcluded': 5}, deadline)
                self.assertEqual(hits, expected)
                # The count asks the same query once, without the exclusions.
                self.assertEqual(store.asked, [('q', None)])
        store = self._Store([])
        self.assertEqual(reader_dispatch._excluded_hits(store, None, ['q', 'r'], excluded, 'deferred_deadline',
                                                        {'vectorExcluded': 5}, deadline), 0)
