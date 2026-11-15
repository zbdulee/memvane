"""Codex lean descriptor preserves policies and rejects actual actions."""
import unittest

from memvane.learning.context_learning.codex_adapter import argv, child_environment, inspect_event


class CodexAdapter(unittest.TestCase):
    def test_lean_descriptor_preserves_policies_and_rejects_actions(self):
        args = argv()
        self.assertEqual(args[args.index('--model') + 1], 'gpt-6-luna')
        self.assertTrue('--ignore-user-config' in args and '--ignore-rules' in args and '--ephemeral' in args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
        self.assertIn('skills.include_instructions=false', args)
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertFalse(any('hooks=false' in a or 'instructions_file' in a or 'base_instructions' in a
                             or 'include_permissions' in a or 'bypass' in a for a in args))
        env = child_environment({'HOME': 'synthetic-home', 'CODEX_HOME': 'existing-auth-location',
                                 'PATH': 'synthetic-path', 'ORCA_AGENT_HOOK_TOKEN': 'secret',
                                 'OPENAI_API_KEY': 'secret', 'CODEX_SESSION_SUGGESTIONS_DEBUG_LOG': 'private'})
        self.assertEqual(env, {'HOME': 'synthetic-home', 'CODEX_HOME': 'existing-auth-location',
                               'PATH': 'synthetic-path', 'QMD_SANDBOX': '1'})
        self.assertEqual(inspect_event({'type': 'item.completed',
                                        'item': {'type': 'agent_message', 'text': 'synthetic'}})['response'],
                         'synthetic')
        self.assertEqual(inspect_event({'type': 'turn.completed', 'usage': {
            'input_tokens': 12, 'cached_input_tokens': 0, 'output_tokens': 3, 'private': 'secret',
            'invalid': -1}})['usage'], {'input_tokens': 12, 'cached_input_tokens': 0, 'output_tokens': 3})
        events = [{'type': 'item.started', 'item': {'type': t}} for t in (
            'command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'collab_tool_call', 'error')]
        events += [{'type': 'hook.started'}, {'type': 'error'}, {'type': 'turn.failed'}, None,
                   {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'x' * 32769}}]
        for event in events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                inspect_event(event)


if __name__ == '__main__':
    unittest.main()
