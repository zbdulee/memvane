"""Candidates cannot overwrite a card belonging to another source or identity."""
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as compiler
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class WriteIdentity(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.config = expanded(self.project, compile={'mode': 'auto-wiki', 'verify': {'enabled': False}})
        use_config(self, self.project, self.config)
        self.project.write_note('docs/a.md', 'first source\n')
        self.project.write_note('docs/b.md', 'second source\n')
        self.card = self.project.wiki / 'entities/existing-a.md'
        self.card.parent.mkdir(parents=True)
        self.card.write_text('---\ntitle: "Existing A"\ncanonicalKey: "existing-a"\n'
                             'type: entity\nstatus: generated\ncreatedBy: memvane\n'
                             'sources:\n  - {kind: file, path: "docs/a.md", collection: "fixture"}\n'
                             '---\n\n<!-- qmd:auto:start id="main" sourceHash="a01da0" -->\n'
                             '## Summary\nOriginal fact.\n<!-- qmd:auto:end -->\n\n'
                             '## Manual notes\nPreserve these notes.\n')
        decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                    'recallSettings': {'skipPaths': []},
                    'collections': [{'name': self.project.wiki_name, 'role': 'wiki',
                                     'sourceRoot': str(self.project.wiki)}]}
        self.bound = (decision, {'productRouteAllowed': True})

    def compile(self, title, key, source):
        candidate = {'trigger': 'post_tool_source', 'engine': 'claude', 'title': title,
                     'canonicalKey': key, 'summary': 'Revised synthetic fact.',
                     'suggestedType': 'entity', 'confidence': 'high',
                     'targetPath': '.memvane/wiki/entities/existing-a.md',
                     'sources': [{'kind': 'file', 'path': source, 'collection': self.project.raw_name}]}
        output = io.StringIO()
        with patch.object(workspace, 'selected', return_value=self.bound), \
             patch.object(workspace, 'recheck'), patch.object(workspace, 'request_sync'), \
             patch.object(workspace, 'append_activity'), redirect_stdout(output):
            compiler._compile_locked(self.project.root, self.config, self.config['compile'],
                                     'auto-wiki', self.project.wiki, candidate, False)
        return json.loads(output.getvalue())

    def test_unrelated_source_is_audited_without_overwriting_card(self):
        before = self.card.read_bytes()
        result = self.compile('Unrelated B', 'unrelated-b', 'docs/b.md')
        self.assertEqual((result['action'], result['reason']), ('merge-needed', 'identity_mismatch'))
        self.assertEqual(self.card.read_bytes(), before)
        queue = self.project.root / '.memvane/compile/merge-needed.jsonl'
        queued = json.loads(queue.read_text().splitlines()[-1])
        self.assertEqual((queued['reason'], queued['candidate']['title']), ('identity_mismatch', 'Unrelated B'))
        audit = self.project.root / '.memvane/compile/candidates.jsonl'
        self.assertEqual(json.loads(audit.read_text().splitlines()[-1])['action'], 'merge-needed')

    def test_same_key_or_shared_source_are_not_identity_mismatches(self):
        for title, key, source in (('Renamed A', 'existing-a', 'docs/a.md'),
                                   ('Different label', 'different-label', 'docs/a.md')):
            with self.subTest(key=key):
                result = self.compile(title, key, source)
                self.assertEqual(result['action'], 'updated')
                self.assertIn('Revised synthetic fact.', self.card.read_text())
                self.assertIn('Preserve these notes.', self.card.read_text())

    def test_refresh_ignores_reviewed_scalar_and_removes_old_verification_spans(self):
        old = self.card.read_text().replace('status: generated\n',
                                          'status: generated\nreviewed: true\nsourceSpans:\n'
                                          '  - {path: "docs/a.md", startLine: 1, endLine: 1}\n')
        self.card.write_text(old)
        result = self.compile('Renamed A', 'existing-a', 'docs/a.md')
        self.assertEqual(result['action'], 'updated')
        new = self.card.read_text()
        self.assertIn('Revised synthetic fact.', new)
        self.assertIn('status: generated', new)
        self.assertNotIn('sourceSpans:', new)
        self.assertNotIn('reviewed:', new)
        self.assertIn('Preserve these notes.', new)

    def test_existing_identity_never_calls_new_page_duplicate_judge(self):
        with patch.object(compiler, 'judge_new_page_duplicate', side_effect=AssertionError('unexpected judge')):
            self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md')['action'], 'updated')

    def test_superseded_and_malformed_cards_are_preserved_for_review(self):
        original = self.card.read_text()
        for content in (original.replace('status: generated', 'status: superseded'),
                        '---\ntitle: malformed\nOriginal text remains without closing frontmatter.\n'):
            with self.subTest(content=content[:30]):
                self.card.write_text(content)
                result = self.compile('Renamed A', 'existing-a', 'docs/a.md')
                self.assertEqual(result['action'], 'merge-needed')
                self.assertEqual(self.card.read_text(), content)

    def test_old_manual_status_is_refreshed_as_generated(self):
        self.card.write_text(self.card.read_text().replace('status: generated', 'status: manual\nreviewed: true'))
        self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md')['action'], 'updated')
        result = self.card.read_text()
        self.assertIn('status: generated', result)
        self.assertNotIn('reviewed:', result)
        self.assertIn('Revised synthetic fact.', result)
