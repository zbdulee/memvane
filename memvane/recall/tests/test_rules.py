"""Recall's verified-card selection rules: pool, answer limit and the selector's reasons."""
import unittest

from memvane.learning.types import SELECTOR_ANSWERS, SELECTOR_CANDIDATE_INVALID
from memvane.recall.domain import rules


class SelectionRules(unittest.TestCase):
    def test_verified_pool_keeps_the_first_fifteen_ranked_rows(self):
        self.assertEqual(rules.verified_pool(list(range(20))), list(range(15)))
        self.assertEqual(rules.verified_pool(list(range(4))), list(range(4)))
        self.assertEqual(rules.verified_pool([]), [])

    def test_answer_limit_is_top_n_capped_at_three(self):
        self.assertEqual(rules.limit_selected(list(range(5)), 5), ([0, 1, 2], 3))
        self.assertEqual(rules.limit_selected(list(range(5)), 2), ([0, 1], 2))
        self.assertEqual(rules.limit_selected([7], 3), ([7], 3))
        self.assertEqual(rules.limit_selected([], 1), ([], 1))

    def test_selection_action_serves_only_chosen_selected_or_ranked_rows(self):
        cases = (('selected', [1], 'serve'), ('ranked', [1], 'serve'),
                 ('selected', [], 'selected_zero'), ('ranked', [], 'empty'),
                 ('no_candidates', [], 'no_candidates'),
                 ('candidate_invalid', [], 'empty'), ('candidate_invalid', [1], 'empty'))
        for reason, chosen, expected in cases:
            with self.subTest(reason=reason, chosen=chosen):
                self.assertEqual(rules.selection_action(reason, chosen), expected)

    def test_selector_reasons_come_from_learning(self):
        self.assertEqual(SELECTOR_ANSWERS, ('selected', 'no_candidates'))
        self.assertEqual(SELECTOR_CANDIDATE_INVALID, 'candidate_invalid')
        for reason in SELECTOR_ANSWERS:
            self.assertEqual(rules.selector_action(reason), 'accept')
        self.assertEqual(rules.selector_action(SELECTOR_CANDIDATE_INVALID), 'invalid')
        for reason in ('candidate_unavailable', 'private_state_unavailable',
                       'shared_trial_not_eligible', 'selector_unavailable',
                       'older_plugin_version', 'ranked'):
            with self.subTest(reason=reason):
                self.assertEqual(rules.selector_action(reason), 'fallback')

    def test_reader_reseal_needs_a_route_and_a_changed_endpoint(self):
        self.assertTrue(rules.reader_reseal_candidate('reader_endpoint_changed', True))
        self.assertFalse(rules.reader_reseal_candidate('reader_endpoint_changed', False))
        self.assertFalse(rules.reader_reseal_candidate('reader_selection_changed', True))


if __name__ == '__main__':
    unittest.main()
