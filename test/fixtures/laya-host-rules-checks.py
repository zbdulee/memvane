from __future__ import annotations
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from memvane.host import host_rules as rules

class RecallRulesChecks(unittest.TestCase):
    def test_pool_preserves_first_fifteen(self):
        self.assertEqual(rules.verified_pool(list(range(20))), list(range(15)))
        self.assertEqual(rules.verified_pool([]), [])

    def test_selected_limit_and_empty_reasons(self):
        self.assertEqual(rules.limit_selected(list(range(5)), 5), ([0, 1, 2], 3))
        self.assertEqual(rules.limit_selected(list(range(5)), 1), ([0], 1))
        for reason, chosen, expected in [
            ('selected', [], 'selected_zero'), ('no_candidates', [], 'no_candidates'),
            ('ranked', [], 'empty'), ('candidate_invalid', [1], 'empty'),
            ('ranked', [1], 'serve'), ('selected', [1], 'serve')]:
            with self.subTest(reason=reason, chosen=chosen):
                self.assertEqual(rules.selection_action(reason, chosen), expected)

    def test_selector_answer_classification(self):
        for reason, expected in [('selected','accept'), ('no_candidates','accept'),
                                 ('candidate_invalid','invalid'), ('unavailable','fallback')]:
            self.assertEqual(rules.selector_action(reason), expected)

    def test_trace_reason_precedence_and_existing_outcome(self):
        cases = [
            (('recall', 1, None, None, 'selected', []), ('served', 'selected')),
            (('recall', 0, None, None, 'no_candidates', ['vector']), ('empty', 'no_candidates')),
            (('recall', 0, None, None, None, ['vector']), ('empty', 'vector')),
            (('recall', 0, None, None, None, []), ('empty', 'no_context')),
            (('recall', 0, 'refused', 'proof', 'selected', ['vector']), ('refused', 'proof')),
            (('index', 0, None, None, 'queued', []), ('ok', 'queued')),
        ]
        for args, expected in cases:
            with self.subTest(args=args): self.assertEqual(rules.trace_result(*args), expected)
        self.assertEqual(rules.selection_final([]), 'no_results_after_filter')
        self.assertEqual(rules.selection_final([{}]), 'selected')

    def test_reseal_requires_matching_endpoint_and_route(self):
        for code, present, expected in [('reader_endpoint_changed', True, True),
                                        ('reader_endpoint_changed', False, False),
                                        ('selected_route_changed', True, False)]:
            self.assertEqual(rules.reader_reseal_candidate(code, present), expected)

class ActionRulesChecks(unittest.TestCase):
    def test_session_and_queued_notice(self):
        for action in ('update', 'update-queued', 'index', 'file-change-event'):
            with self.subTest(action=action):
                self.assertEqual(rules.is_session(action), action in ('update', 'update-queued'))

    def test_grant_is_scoped_to_collection_and_action(self):
        for roots, action, expected in [(False, 'update', None), (True, 'index', None),
                (True, 'file-change-event', 'sync'), (True, 'update', 'session_start'),
                (True, 'update-queued', 'session_start')]:
            with self.subTest(roots=roots, action=action):
                self.assertEqual(rules.discovery_grant_kind(roots, action), expected)

    def test_gate_response_for_host(self):
        for host, expected in [('hermes', {'action': 'continue'}), ('claude', {}), ('codex', {})]:
            with self.subTest(host=host):
                self.assertEqual(rules.gate_response(host), expected)

    def test_compile_queue_observation_only_for_session_with_collections(self):
        for collections in (False, True):
            for action in ('update', 'update-queued', 'index', 'file-change-event'):
                with self.subTest(collections=collections, action=action):
                    self.assertEqual(rules.check_compile_queue(collections, action),
                                     collections and action in ('update', 'update-queued'))

if __name__ == '__main__': unittest.main()
