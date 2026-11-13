#!/usr/bin/env python3
"""Synthetic route proofs exercise selected gate responses."""
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memvane.host import host_response as response
from memvane.journal import install_journal as journal


class GateResponseChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-gate-')
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name).resolve())
        self.decision = {'state': 'candidate', 'projectRoot': self.root,
                         'pluginRoot': str(Path(response.__file__).resolve().parents[2])}
        self.proof = {'productRouteAllowed': True, 'stateFormat': 1, 'writerFloor': '1.0.0'}
        self.event = {'cwd': self.root, 'hook_event_name': 'PreToolUse',
                      'tool_name': 'Edit', 'tool_input': {'file_path': 'note.md'}}

    def call_gate(self, host, event=None):
        with patch.object(journal, 'verify_selected_route', return_value=self.proof):
            result = response.serve_selected('gate', host, self.decision,
                                             self.event if event is None else event)
        return json.loads(json.dumps(result))

    def test_selected_codex_allow_is_empty(self):
        self.assertEqual(self.call_gate('codex'), {})

    def test_selected_claude_allow_is_empty(self):
        self.assertEqual(self.call_gate('claude'), {})

    def test_selected_proof_change_rejected(self):
        for host in ('codex', 'claude', 'hermes'):
            with self.subTest(host=host):
                with patch.object(journal, 'verify_selected_route',
                                  side_effect=[self.proof, {'productRouteAllowed': True, 'changed': True}]):
                    with self.assertRaisesRegex(response.HostResponseError, 'selected_proof_changed'):
                        response.serve_selected('gate', host, self.decision, self.event)

    def test_worktree_codex_allow_is_empty(self):
        main = {**self.decision}
        decision = {'state': 'worktree', 'mainRoute': main}
        with patch.object(journal, 'verify_selected_route',
                          return_value=self.proof):
            self.assertEqual(response.serve_selected('gate', 'codex', decision, self.event), {})

    def test_worktree_claude_allow_is_empty(self):
        decision = {'state': 'worktree', 'mainRoute': self.decision}
        with patch.object(journal, 'verify_selected_route',
                          return_value=self.proof):
            self.assertEqual(response.serve_selected('gate', 'claude', decision, self.event), {})

    def test_worktree_other_hosts_and_proof_rejection(self):
        decision = {'state': 'worktree', 'mainRoute': self.decision}
        with patch.object(journal, 'verify_selected_route',
                          return_value=self.proof):
            self.assertEqual(response.serve_selected('gate', 'hermes', decision, self.event),
                             {'action': 'continue'})
        with patch.object(journal, 'verify_selected_route',
                          return_value={**self.proof, 'productRouteAllowed': False}):
            for host in ('codex', 'claude', 'hermes'):
                with self.assertRaisesRegex(response.HostResponseError, 'worktree_selection_unapproved'):
                    response.serve_selected('gate', host, decision, self.event)

    def test_invalid_gate_exits_three_without_output_for_both_hosts(self):
        event = {**self.event, 'hook_event_name': 'PostToolUse'}
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                data = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()), encoding='utf8')
                out, err = io.StringIO(), io.StringIO()
                with patch.object(sys, 'stdin', data), \
                     patch.object(response, 'select_hook_payload', return_value=self.decision), \
                     patch.object(response, '_reselect', return_value=self.decision), \
                     patch.object(journal, 'verify_selected_route', return_value=self.proof), \
                     patch.object(response.event_log, 'emit'), \
                     redirect_stdout(out), redirect_stderr(err):
                    code = response.main(['serve-stdin', 'gate', host, self.root])
                self.assertEqual(code, 3)
                self.assertEqual(out.getvalue(), '')
                self.assertIn('invalid_edit_event', err.getvalue())

    def test_other_hosts_and_invalid_events_keep_their_behavior(self):
        self.assertEqual(self.call_gate('hermes'), {'action': 'continue'})
        for host in ('codex', 'claude', 'hermes'):
            with self.assertRaisesRegex(response.HostResponseError, 'invalid_edit_event'):
                self.call_gate(host, {**self.event, 'hook_event_name': 'PostToolUse'})


if __name__ == '__main__':
    print('child-python=' + os.path.realpath(sys.executable) + ' version=' + sys.version.split()[0], flush=True)
    unittest.main()
