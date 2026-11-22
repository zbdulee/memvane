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
        real = reader_dispatch.subprocess.Popen
        with mock.patch.object(reader_dispatch.subprocess, 'Popen', side_effect=real) as popen, \
                mock.patch('memvane.recall.recall_scope._wake_provider') as wake:
            context, _err, event = self.recall(prompt)
        return context, event, popen.call_count, wake.call_count

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
