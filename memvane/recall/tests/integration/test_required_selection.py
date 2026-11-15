"""Selection over published cards: a prepared selector decides the bounded pool, else hybrid rank.

Twenty verified cards all match the prompt. The selector seams are the
learning api (``selector_prepared``, ``select_published``,
``published_candidate_rows``); everything else is the real selected recall.
"""
import unittest
from unittest import mock

from memvane.recall.tests.support import SHARED, TOPICS, RecallInstallCase

PROMPT = f'which protocol keeps the {SHARED} marker?'
ALL = [f'concepts/{topic}.md' for topic in TOPICS]


def served(context):
    """Card paths of the rendered context, in order."""
    return [path for line in context.split('\n') if line.startswith('- "')
            for path in [line.split('"')[1]]]


class RequiredSelection(RecallInstallCase):
    CARDS = 20

    def select(self, choose):
        """Patch a prepared selector whose answer is ``choose(pool)``; returns the pools it saw."""
        pools = []

        def select_published(pool, **kwargs):
            pools.append(list(pool))
            return choose(pool)
        for target, kwargs in (('memvane.learning.api.selector_prepared', {'return_value': True}),
                               ('memvane.learning.api.select_published',
                                {'side_effect': select_published})):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        return pools

    def test_a_prepared_selector_decides_the_bounded_pool(self):
        pools = self.select(lambda pool: {'reason': 'selected', 'final_results': [pool[14], pool[3]]})
        context, _err, event = self.recall(PROMPT)
        self.assertEqual(len(pools), 1)
        self.assertEqual(len(pools[0]), 15)
        self.assertEqual(served(context), [pools[0][14]['path'], pools[0][3]['path']])
        self.assertNotIn(pools[0][0]['path'], served(context))
        self.assertIn('local Laya model selected', context)
        self.assertEqual((event['outcome'], event['count']), ('served', 2))
        self.assertIsNone(event.get('selection_fallback'))

    def test_a_selected_zero_or_invalid_pool_serves_nothing(self):
        for reason, chosen, note in (('selected', [], 'laya_selector_selected_zero'),
                                     ('candidate_invalid', None, 'laya_selector_candidate_invalid')):
            with self.subTest(reason=reason):
                with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                        mock.patch('memvane.learning.api.select_published',
                                   side_effect=lambda pool, **_k: {
                                       'reason': reason,
                                       'final_results': chosen if chosen is not None else pool[:1]}):
                    context, _err, event = self.recall(PROMPT)
                self.assertEqual(context, '')
                self.assertEqual(event['outcome'], 'empty')
                self.assertIn(note, event.get('notes') or [])

    def test_an_unprepared_selector_serves_the_ranked_rows(self):
        with mock.patch('memvane.learning.api.selector_prepared', return_value=False), \
                mock.patch('memvane.learning.api.select_published',
                           side_effect=AssertionError('an unprepared selector is never asked')):
            context, _err, event = self.recall(PROMPT, session='ranked')
        self.assertEqual(len(served(context)), 3)
        self.assertTrue(all(path.endswith(tuple(ALL)) for path in served(context)), context)
        self.assertIn('hybrid rank score', context)
        self.assertNotIn('local Laya model selected', context)
        self.assertEqual((event['outcome'], event['reason'], event['count']), ('served', 'selected', 3))
        self.assertEqual(event['selection_fallback'], 'laya_selector_not_prepared')
        self.assertEqual([card['path'] for card in event['cards']], served(context))
        self.assertTrue(all(len(card['sha']) == 64 for card in event['cards']))
        self.assertEqual(set(event['prompt']), {'sha256', 'chars'})

    def test_a_prepared_selector_that_cannot_answer_serves_the_ranked_rows(self):
        for reason in ('candidate_unavailable', 'private_state_unavailable', 'selector_unavailable'):
            with self.subTest(reason=reason):
                with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                        mock.patch('memvane.learning.api.select_published',
                                   return_value={'reason': reason, 'final_results': []}):
                    context, err, event = self.recall(PROMPT)
                self.assertEqual(len(served(context)), 3)
                self.assertIn('hybrid rank score', context)
                self.assertIn('laya_selector_' + reason, event.get('notes') or [])
                self.assertEqual(event['selection_fallback'], 'laya_selector_' + reason)
                self.assertIn('local selection unavailable (' + reason + ')', err)
        with mock.patch('memvane.learning.api.selector_prepared', return_value=True), \
                mock.patch('memvane.learning.api.select_published', side_effect=RuntimeError('crash')):
            context, _err, event = self.recall(PROMPT)
        self.assertEqual(len(served(context)), 3)
        self.assertEqual(event['selection_fallback'], 'laya_selector_selector_unavailable')

    def test_an_invalid_published_pool_is_never_served_ranked(self):
        with mock.patch('memvane.learning.api.selector_prepared', return_value=False), \
                mock.patch('memvane.learning.api.published_candidate_rows',
                           side_effect=ValueError('selector_candidate_invalid')):
            context, err, event = self.recall(PROMPT)
        self.assertEqual(context, '')
        self.assertIn('laya_selector_candidate_invalid', event.get('notes') or [])
        self.assertIn('published candidates invalid; no context injected', err)


if __name__ == '__main__':
    unittest.main()
