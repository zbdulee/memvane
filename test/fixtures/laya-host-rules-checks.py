from __future__ import annotations
import sys
import unittest
import io
import json
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_host_rules as rules

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

    def test_upgrade_transition_codes_and_fence(self):
        for code in ('selected_route_changed', 'selected_route_unavailable', 'selected_proof_changed',
                     'incremental_base_changed', 'host_route_unverified', 'host_decision_changed'):
            self.assertTrue(rules.is_upgrade_transition(code))
        for code in ('reader_endpoint_changed', 'selected_proof_rolled_back', ''):
            self.assertFalse(rules.is_upgrade_transition(code))
        self.assertEqual(rules.fence_reason(True), 'bundle_upgrade_stalled')
        self.assertEqual(rules.fence_reason(False), 'bundle_upgrade_in_progress')

    def test_reseal_requires_matching_endpoint_and_route(self):
        for code, present, expected in [('reader_endpoint_changed', True, True),
                                        ('reader_endpoint_changed', False, False),
                                        ('selected_route_changed', True, False)]:
            self.assertEqual(rules.reader_reseal_candidate(code, present), expected)

    def test_upgrade_notice_keeps_fence_and_window_observation_order(self):
        import laya_fresh_wiki_origin as fresh
        import laya_host_response as host
        with patch.object(fresh, 'upgrade_fenced', return_value=True) as fenced, \
             patch.object(fresh, 'upgrade_stalled', return_value=True) as stalled, \
             patch.object(fresh, 'upgrade_window') as window:
            self.assertEqual(host._upgrade_recall_notice({'cwd': '/p'}, '/fallback', 'other'),
                             host._UPGRADE_NOTICES['bundle_upgrade_stalled'])
            fenced.assert_called_once_with('/p')
            stalled.assert_called_once_with('/p')
            window.assert_not_called()
        with patch.object(fresh, 'upgrade_fenced', return_value=False), \
             patch.object(fresh, 'upgrade_window', return_value=True) as window:
            self.assertIsNone(host._upgrade_recall_notice({'cwd': '/p'}, '/fallback', 'other'))
            window.assert_not_called()
            self.assertEqual(host._upgrade_recall_notice({'cwd': '/p'}, '/fallback', 'selected_route_changed'),
                             host._UPGRADE_NOTICES['bundle_upgrade_in_progress'])
            window.assert_called_once_with('/p')

    def test_main_returns_zero_only_for_upgrade_transition(self):
        import hook_budget
        import laya_fresh_wiki_origin as fresh
        import laya_host_response as host
        payload = json.dumps({'cwd': '/p', 'prompt': 'q'}).encode()
        for code, expected in [('selected_route_changed', 0), ('selected_proof_rolled_back', 3)]:
            with self.subTest(code=code), \
                 patch.object(hook_budget, 'arm'), \
                 patch.object(host, 'select_hook_payload', return_value={'state': 'candidate'}), \
                 patch.object(host, '_reselect', side_effect=host.HostResponseError(code)), \
                 patch.object(fresh, 'upgrade_fenced', return_value=False), \
                 patch.object(fresh, 'upgrade_window', return_value=True) as window, \
                 patch('sys.stdin', SimpleNamespace(buffer=io.BytesIO(payload))), \
                 patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(host.main(['serve-stdin', 'recall', 'claude', '/p']), expected)
                self.assertEqual(window.call_count, 1 if expected == 0 else 0)

class ActionRulesChecks(unittest.TestCase):
    def test_bundle_drift_actions(self):
        for action, expected in [('recall', 'recall'), ('update', 'session'),
                ('update-queued', 'session'), ('gate', 'gate'), ('index', 'defer'),
                ('posttool', 'defer'), ('compile', 'defer'), ('file-change-event', 'defer')]:
            with self.subTest(action=action):
                self.assertEqual(rules.drift_action(action), expected)

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
