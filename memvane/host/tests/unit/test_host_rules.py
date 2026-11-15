"""Pure host decisions: trace outcome and reason, session actions, grants, gate and compile queue.

Recall's own selection rules (pool, answer limit, selector reasons, reseal) are
in ``memvane/recall/tests/unit/test_rules.py``.
"""
import unittest

from memvane.host import host_rules as rules


class TraceRules(unittest.TestCase):
    def test_trace_reason_precedence_keeps_an_existing_outcome(self):
        cases = [
            (('recall', 1, None, None, 'selected', []), ('served', 'selected')),
            (('recall', 0, None, None, 'no_candidates', ['vector']), ('empty', 'no_candidates')),
            (('recall', 0, None, None, None, ['vector']), ('empty', 'vector')),
            (('recall', 0, None, None, None, []), ('empty', 'no_context')),
            (('recall', 0, 'refused', 'proof', 'selected', ['vector']), ('refused', 'proof')),
            (('index', 0, None, None, 'queued', []), ('ok', 'queued')),
            (('gate', 0, 'refused', 'invalid_edit_event', None, []), ('refused', 'invalid_edit_event')),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(rules.trace_result(*args), expected)

    def test_selection_final_names_an_empty_filter_result(self):
        self.assertEqual(rules.selection_final([]), 'no_results_after_filter')
        self.assertEqual(rules.selection_final([{}]), 'selected')


class ActionRules(unittest.TestCase):
    ACTIONS = ('update', 'update-queued', 'index', 'file-change-event')

    def test_session_actions(self):
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(rules.is_session(action), action in ('update', 'update-queued'))

    def test_discovery_grant_is_scoped_to_collections_and_action(self):
        for roots, action, expected in [(False, 'update', None), (True, 'index', None),
                                        (True, 'file-change-event', 'sync'),
                                        (True, 'update', 'session_start'),
                                        (True, 'update-queued', 'session_start')]:
            with self.subTest(roots=roots, action=action):
                self.assertEqual(rules.discovery_grant_kind(roots, action), expected)

    def test_gate_response_per_host(self):
        for host, expected in [('hermes', {'action': 'continue'}), ('claude', {}), ('codex', {})]:
            with self.subTest(host=host):
                self.assertEqual(rules.gate_response(host), expected)

    def test_compile_queue_is_observed_only_for_sessions_with_collections(self):
        for collections in (False, True):
            for action in self.ACTIONS:
                with self.subTest(collections=collections, action=action):
                    self.assertEqual(rules.check_compile_queue(collections, action),
                                     collections and action in ('update', 'update-queued'))


if __name__ == '__main__':
    unittest.main()
