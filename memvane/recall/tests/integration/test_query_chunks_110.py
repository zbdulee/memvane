"""A long prompt is asked as up to four windows in one embedding child.

The window rule is checked on its own (``embed_chunks`` with a recording
``embed``); the recalls run ``RecallInstallCase`` with an admitted fixture
provider that logs the length of every query text it is asked.
"""
from __future__ import annotations

from unittest import mock

from memvane.provider import query_embed_child as child
from memvane.recall import reader_dispatch
from memvane.recall.tests.support import RecallInstallCase
import unittest

PROMPT = 'what marker does the heron protocol keep?'


def long_prompt(chars):
    filler = ' The osprey ledger adds nothing to this question.'
    text = PROMPT + filler * (chars // len(filler) + 1)
    return text[:chars - len(PROMPT) - 1] + ' ' + PROMPT


class Windows(unittest.TestCase):
    def run_chunks(self, prompt, *, refuse_over=None, budget=None):
        asked, spent = [], [0]

        def embed(text, seconds):
            asked.append(text)
            if refuse_over is not None and len(text) > refuse_over:
                raise ValueError('provider_result_invalid')
            return [float(len(asked))]

        def left():
            spent[0] += 1
            return 1.0 if budget is None or spent[0] <= budget else 0.0
        vectors, requested, error = child.embed_chunks(prompt, embed, left)
        return asked, vectors, requested, error

    def test_a_prompt_up_to_the_whole_bound_is_asked_once(self):
        for chars in (1, 2000, 6000):
            asked, vectors, requested, error = self.run_chunks('x' * chars)
            self.assertEqual(([len(text) for text in asked], len(vectors), requested, error), ([chars], 1, 1, None))

    def test_a_longer_prompt_is_asked_as_its_last_three_windows_and_its_first(self):
        prompt = ''.join(chr(0x4e00 + index % 2000) for index in range(12000))
        asked, vectors, requested, error = self.run_chunks(prompt)
        self.assertEqual(asked, [prompt[10800:], prompt[9600:10800], prompt[8400:9600], prompt[:1200]])
        self.assertEqual((len(vectors), requested, error), (4, 4, None))
        asked, _vectors, _requested, _error = self.run_chunks('y' * 6001)
        self.assertEqual([len(text) for text in asked], [1200, 1200, 1200, 1200])

    def test_a_failed_whole_request_over_the_retry_bound_goes_on_as_windows(self):
        prompt = ''.join(chr(0x4e00 + index % 2000) for index in range(5000))
        asked, vectors, requested, error = self.run_chunks(prompt, refuse_over=1500)
        self.assertEqual(asked, [prompt, prompt[3800:], prompt[2600:3800], prompt[1400:2600], prompt[:1200]])
        self.assertEqual((len(vectors), requested, error), (4, 5, 'provider_result_invalid'))
        # Windows cover a prompt shorter than four of them without overlap.
        asked, _vectors, _requested, _error = self.run_chunks('z' * 3000, refuse_over=1500)
        self.assertEqual([len(text) for text in asked], [3000, 1200, 1200, 600])
        # A short prompt's failure is a failure: no windows.
        asked, vectors, requested, error = self.run_chunks('z' * 2000, refuse_over=10)
        self.assertEqual((len(asked), vectors, requested, error), (1, [], 1, 'provider_result_invalid'))

    def test_a_failed_window_is_asked_once_more_as_its_second_half(self):
        prompt = ''.join(chr(0x4e00 + index % 2000) for index in range(12000))
        asked, vectors, requested, error = self.run_chunks(prompt, refuse_over=700)
        self.assertEqual(asked, [prompt[10800:], prompt[11400:], prompt[9600:10800], prompt[10200:10800],
                                 prompt[8400:9600], prompt[9000:9600], prompt[:1200], prompt[600:1200]])
        self.assertEqual((len(vectors), requested, error), (4, 8, 'provider_result_invalid'))
        asked, vectors, requested, error = self.run_chunks(prompt, refuse_over=10)
        self.assertEqual((len(asked), vectors, requested, error), (8, [], 8, 'provider_result_invalid'))

    def test_windows_left_when_the_time_ends_are_dropped(self):
        asked, vectors, requested, error = self.run_chunks('w' * 12000, budget=2)
        self.assertEqual((len(asked), len(vectors), requested, error), (2, 2, 2, None))
        asked, vectors, requested, _error = self.run_chunks('w' * 12000, budget=0)
        self.assertEqual((asked, vectors, requested), ([], [], 0))


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
