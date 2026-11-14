import unittest
from memvane.wiki.engines.extractors import lib


class ExtractorPromptTests(unittest.TestCase):
    def prompt(self, **values):
        return lib.build_prompt({'source': {'path': 'docs/x.md', 'content': 'BODY'},
                                 'wiki': {'schema': 'S'}, **values})

    def test_verbatim_rules_preserve_identifiers_and_do_not_invent_them(self):
        prompt = self.prompt()
        for text in ('VERBATIM RULES', 'backtick code spans', 'file and directory paths',
                     'do NOT shorten it to a basename', 'signatures',
                     'configuration keys, field names, env vars, CLI flags',
                     'numeric thresholds, limits, versions, percentages',
                     'error strings, status/reason codes, log lines, exit codes',
                     'bracketed or prefixed tags and IDs', 'Do NOT invent identifiers'):
            with self.subTest(text=text):
                self.assertIn(text.lower(), prompt.lower())

    def test_transcript_is_excluded_without_rewarding_brevity(self):
        prompt = self.prompt()
        self.assertNotIn('compact, durable wiki candidates', prompt)
        self.assertNotIn('short durable conclusion', prompt)
        for text in ('There is no reward for brevity', 'never drop an identifier to save space',
                     'NOT a transcript, NOT step-by-step dialog, NOT a scene-by-scene retelling'):
            self.assertIn(text, prompt)

    def test_narrative_identifiers_and_secrets_are_handled_explicitly(self):
        prompt = self.prompt()
        for text in ('names of people, places, entities and short quoted lines',
                     'reproduce the wording exactly', 'EP13',
                     'Never include secrets, API keys, tokens, or credentials — this OUTRANKS the verbatim rules'):
            self.assertIn(text, prompt)

    def test_line_limit_uses_valid_input_and_defaults(self):
        for value, limit in ((40, 40), ('nope', 120), (0, 120)):
            with self.subTest(value=value):
                self.assertIn('up to %d lines' % limit, self.prompt(maxLines=value))
        self.assertIn('up to 120 lines', self.prompt())

    def test_truncation_is_visible_only_when_true(self):
        source = {'path': 'docs/x.md', 'content': 'BODY'}
        truncated = lib.build_prompt({'source': {**source, 'truncated': True}, 'wiki': {}})
        complete = lib.build_prompt({'source': {**source, 'truncated': False}, 'wiki': {}})
        self.assertIn('SOURCE FILE: docs/x.md (truncated: true', truncated)
        self.assertIn('never assert that something is absent from the document', truncated)
        self.assertIn('SOURCE FILE: docs/x.md\n', complete)
        self.assertNotIn('truncated: true', complete)
