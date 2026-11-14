"""Target resolution and lint are bounded before a wiki write."""
from memvane.wiki.compile import wiki_compile as compile_rule
from tests.support.case import FixtureCase
from tests.support.project import make_project


class CompileDecisions(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root, self.wiki = self.project.root, self.project.wiki

    def test_wiki_relative_target_and_invalid_type_or_extension(self):
        candidate = {'title': 'Target Rule', 'targetPath': 'concepts/target.md'}
        target, reason, _ = compile_rule.resolve_target(self.root, self.wiki, candidate, 'concept', {})
        self.assertEqual((target, reason), (self.wiki / 'concepts/target.md', 'explicit'))
        for path in ('decisions/wrong.md', 'concepts/wrong.txt'):
            with self.subTest(path=path):
                target, reason, _ = compile_rule.resolve_target(
                    self.root, self.wiki, {**candidate, 'targetPath': path}, 'concept', {})
                self.assertEqual((target, reason), (self.wiki / 'concepts/target-rule.md', 'slug'))

    def test_traversal_absolute_and_hidden_targets_are_unsafe(self):
        for path in ('../outside.md', 'concepts/../../../escape.md',
                     '/tmp/evil-absolute.md', 'concepts/.hidden/foo.md'):
            with self.subTest(path=path):
                target, reason, _ = compile_rule.resolve_target(
                    self.root, self.wiki, {'title': 'Unsafe', 'targetPath': path}, 'concept', {})
                self.assertIsNone(target)
                self.assertEqual(reason, 'unsafe')
                self.assertIn('unsafe_target_path', compile_rule.lint_candidate(
                    {'title': 'Unsafe', 'summary': 'Synthetic content'}, target, 120)['findings'])

    def test_identity_match_reuses_card_and_ambiguous_match_requires_review(self):
        first = self.wiki / 'concepts/first.md'
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text('---\ntitle: Original\ncanonicalKey: shared-key\naliases:\n  - Alias\n---\nBody\n')
        index = compile_rule.build_identity_index(self.wiki)
        candidate = {'title': 'Renamed', 'canonicalKey': 'shared-key'}
        target, reason, _ = compile_rule.resolve_target(self.root, self.wiki, candidate, 'concept', index)
        self.assertEqual((target, reason), (first, 'canonicalKey'))
        candidate = {'title': 'Alias'}
        target, reason, _ = compile_rule.resolve_target(self.root, self.wiki, candidate, 'concept', index)
        self.assertEqual((target, reason), (first, 'title'))
        second = first.with_name('second.md')
        second.write_text(first.read_text())
        target, reason, matches = compile_rule.resolve_target(
            self.root, self.wiki, {'canonicalKey': 'shared-key'}, 'concept',
            compile_rule.build_identity_index(self.wiki))
        self.assertIsNone(target)
        self.assertEqual(reason, 'ambiguous_canonicalKey')
        self.assertEqual(set(matches), {first, second})

    def test_transcript_and_secret_are_rejected_and_redacted(self):
        secret = 'sk-' + '1234567890abcdef' * 2
        text = 'User: save this\nAssistant: ok\n' + secret
        lint = compile_rule.lint_candidate({'title': 'Bad', 'summary': text}, self.wiki / 'x.md', 120)
        self.assertEqual(lint['verdict'], 'reject')
        self.assertTrue({'transcript_like', 'secret_like'} <= set(lint['findings']))
        redacted, reasons = compile_rule.redact(text)
        self.assertNotIn(secret, redacted)
        self.assertIn('secret_like', reasons)
