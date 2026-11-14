import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from memvane.wiki.engines import api


class EnginePolicyTests(unittest.TestCase):
    def test_budget_is_read_from_one_block(self):
        self.assertEqual(api.budget_of({'budget': {'extractorPerRun': 4}}),
                         {'extractorPerRun': 4})
        self.assertEqual(api.budget_of({'budget': None}), {})

    def test_source_decoding_is_bounded_and_rejects_invalid_utf8(self):
        self.assertEqual(api.decode_source_bounded(b'abcdef', 3), ('abc', True))
        self.assertEqual(api.decode_source_bounded(b'abc', 3), ('abc', False))
        self.assertIsNone(api.decode_source_bounded(b'\xff', 3))

    def test_unknown_extractor_is_not_resolved(self):
        self.assertIsNone(api.resolve_extractor_argv({'extractor': {'builtins': ['claude']}},
                                                     'unknown'))

    def test_declared_builtin_resolves_to_the_packaged_entry(self):
        argv = api.resolve_extractor_argv({'extractor': {'builtins': ['claude']}}, 'claude')
        self.assertIsInstance(argv, list)
        self.assertTrue(argv)
        self.assertIn('extractor-claude', argv)

    def test_effort_audit_keeps_only_bounded_policy_fields(self):
        self.assertEqual(api.reasoning_effort_audit(
            {'requested': 'high', 'applied': 'high', 'status': 'applied', 'reason': 'ok'},
            capability_declared=True),
            {'requested': 'high', 'applied': 'high', 'status': 'applied', 'reason': 'ok'})
        rejected = api.reasoning_effort_audit({'requested': 'secret', 'applied': 'secret'},
                                              capability_declared=False)
        self.assertEqual(rejected['status'], 'unsupported')
        self.assertNotIn('secret', str(rejected))

    def test_existing_context_uses_only_matching_pages(self):
        rendered = api.render_existing_context_section({'similarPages': [
            {'path': 'wiki/card.md', 'score': 0.9, 'content': 'stable fact'}]})
        self.assertIn('wiki/card.md', rendered)
        self.assertIn('stable fact', rendered)
        self.assertEqual(api.render_existing_context_section({'similarPages': []}), '')

    def test_extractor_candidate_parser_ignores_prose_and_keeps_json_objects(self):
        from memvane.wiki.engines.extractors import lib
        parsed = lib.extract_candidates('answer: {"candidates": [{"title": "A"}]}')
        self.assertEqual(parsed, {'candidates': [{'title': 'A'}]})
        self.assertEqual(lib.extract_candidates('not json'), {})

    def test_verdict_parser_uses_last_valid_adversarial_result(self):
        from memvane.wiki.engines.extractors import lib
        parsed = lib.extract_verdict('{"verdict":"pass"} {"verdict":"invalid"}')
        self.assertEqual(parsed['verdict'], 'pass')

    def test_adapter_commands_restrict_tools_and_persistence(self):
        from memvane.wiki.engines.extractors import claude_adapter, codex_adapter, hermes_adapter
        claude = claude_adapter.build_command('cli', 'prompt', 'high')
        codex = codex_adapter.build_command('cli', 'prompt', 'high')
        hermes = hermes_adapter.build_command('cli', 'prompt')
        for flag in ('--safe-mode', '--no-session-persistence', '--tools'):
            self.assertIn(flag, claude)
        for flag in ('read-only', '--ephemeral', '--ignore-user-config', '--ignore-rules'):
            self.assertIn(flag, codex)
        for flag in ('-z', '--safe-mode', '--ignore-user-config', '--ignore-rules'):
            self.assertIn(flag, hermes)
        self.assertIn('high', claude)
        self.assertIn('model_reasoning_effort=high', codex)

    def test_extraction_and_verification_prompts_include_required_evidence(self):
        from memvane.wiki.engines.extractors import lib
        prompt = lib.build_prompt({'source': {'path': 'docs/x.md', 'content': 'SOURCE'},
                                   'wiki': {'schema': 'RULES'}})
        for term in ('SOURCE', 'candidates', 'canonicalKey', 'aliases', 'targetPath'):
            self.assertIn(term, prompt)
        verify = lib.build_verify_prompt({'card': {'path': 'wiki/x.md', 'content': 'CARD'},
                                          'sources': [{'path': 'docs/x.md', 'content': 'SOURCE',
                                                       'truncated': True}]})
        for term in ('CARD', 'SOURCE', 'truncated: true', '"verdict"', 'REFUTE'):
            self.assertIn(term, verify)

    def test_candidate_parser_handles_braces_inside_quoted_strings(self):
        from memvane.wiki.engines.extractors import lib
        parsed = lib.extract_candidates('text {"candidates":[{"summary":"function foo() {"}]}')
        self.assertEqual(parsed['candidates'][0]['summary'], 'function foo() {')

    def test_extractor_preserves_absent_and_other_failure_codes(self):
        with patch('memvane.runtime.api.provider_run', side_effect=FileNotFoundError):
            self.assertEqual(api.run_extractor(['missing'], {}, 10, Path('/tmp')),
                             (None, 'extractor_failed', 127))
        with patch('memvane.runtime.api.provider_run', side_effect=PermissionError):
            self.assertEqual(api.run_extractor(['unrunnable'], {}, 10, Path('/tmp')),
                             (None, 'extractor_failed', None))
        for code in (1, 2, 127):
            with self.subTest(code=code):
                proc = SimpleNamespace(returncode=code, stdout='', stderr='')
                with patch('memvane.runtime.api.provider_run', return_value=proc):
                    self.assertEqual(api.run_extractor(['fake'], {}, 10, Path('/tmp')),
                                     (None, 'extractor_failed', code))


if __name__ == '__main__':
    unittest.main()
