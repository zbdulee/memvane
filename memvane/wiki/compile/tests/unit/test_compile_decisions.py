"""Target resolution and lint are bounded before a wiki write."""
from memvane.wiki.compile import wiki_compile as compile_rule
from memvane.wiki.compile import wiki_compile_worker as worker
from memvane.wiki.workspace import api as workspace
from unittest.mock import patch
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

    def test_root_meta_pages_are_not_identity_or_explicit_targets(self):
        text = '---\ntitle: Shared\ncanonicalKey: shared\n---\nuntouched\n'
        for name in ('index.md', 'log.md'):
            (self.wiki / name).write_text(text)
        index = compile_rule.build_identity_index(self.wiki)
        candidate = {'title': 'Shared', 'canonicalKey': 'shared'}
        for target_path in (None, '.memvane/wiki/index.md', '.memvane/wiki/log.md'):
            with self.subTest(target_path=target_path):
                value = {**candidate, **({'targetPath': target_path} if target_path else {})}
                target, reason, _ = compile_rule.resolve_target(self.root, self.wiki, value, 'concept', index)
                self.assertEqual((target, reason), (self.wiki / 'concepts/shared.md', 'slug'))
        for name in ('index.md', 'log.md'):
            self.assertEqual((self.wiki / name).read_text(), text)

    def test_bundled_orientation_ignores_root_files_and_detects_novel_layout(self):
        for name in ('SCHEMA.md', 'index.md', 'log.md'):
            (self.wiki / name).write_text('PRIVATE_ROOT_SENTINEL')
        with patch.object(workspace, 'activity_tail', return_value='selected activity'):
            default = worker.orientation(self.root, self.wiki, {}, None)
            self.assertEqual(default['schema'], worker.card_rules('default'))
            self.assertEqual(default['logTail'], 'selected activity')
            self.assertNotIn('PRIVATE_ROOT_SENTINEL', repr(default))
            (self.root / '04_Manuscript').mkdir()
            novel = worker.orientation(self.root, self.wiki, {}, None)
            self.assertEqual(novel['schema'], worker.card_rules('novel'))
            self.assertNotEqual(novel['schema'], default['schema'])
            self.assertNotIn('PRIVATE_ROOT_SENTINEL', repr(novel))

    def test_novel_detection_rejects_near_matches_files_and_symlinks(self):
        for name in ('Manuscript', '04_manuscript', '04_Manuscript_extra'):
            (self.root / name).mkdir()
        (self.root / '03_Manuscript').write_text('not a directory')
        (self.root / '05_Manuscript').symlink_to(self.root / 'Manuscript', target_is_directory=True)
        self.assertEqual(worker.card_rules_preset(self.root, self.wiki, {}), 'default')
        (self.wiki / 'characters').mkdir()
        self.assertEqual(worker.card_rules_preset(self.root, self.wiki, {}), 'novel')

    def test_frontmatter_updates_only_named_fields_and_rejects_unparseable_text(self):
        page = self.wiki / 'page.md'
        original = ('---\ntitle: "Some Decision"\ncanonicalKey: "some-decision"\n'
                    'aliases:\n  - "Alt Name"\nstatus: generated\ncreatedBy: memvane\n'
                    '---\n\nBody text must survive.\n')
        page.write_text(original)
        self.assertTrue(compile_rule.patch_frontmatter_fields(
            page, {'status': 'superseded', 'supersededBy': '.memvane/wiki/decisions/new.md'}))
        expected = original.replace('status: generated', 'status: superseded').replace(
            'createdBy: memvane\n', 'createdBy: memvane\nsupersededBy: ".memvane/wiki/decisions/new.md"\n')
        self.assertEqual(page.read_text(), expected)
        self.assertTrue(compile_rule.patch_frontmatter_fields(page, {'status': 'needs review'}))
        self.assertIn('status: "needs review"', page.read_text())
        page.write_text('no frontmatter here\n')
        self.assertFalse(compile_rule.patch_frontmatter_fields(page, {'status': 'superseded'}))
        self.assertEqual(page.read_text(), 'no frontmatter here\n')
