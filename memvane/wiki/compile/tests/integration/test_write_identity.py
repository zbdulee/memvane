"""Candidates cannot overwrite a card belonging to another source or identity."""
import io
import json
import hashlib
from contextlib import redirect_stdout
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as compiler
from memvane.wiki.card import api as card
from memvane.wiki.workspace import api as workspace
from tests.support.wiki_handoff import verify_rows
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class WriteIdentity(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home)
        self.config = expanded(self.project, compile={'mode': 'auto-wiki', 'verify': {'enabled': True}})
        use_config(self, self.project, self.config)
        source_a = self.project.write_note('docs/a.md', 'first source\n')
        self.project.write_note('docs/b.md', 'second source\n')
        self.card = self.project.wiki / 'entities/existing-a.md'
        self.card.parent.mkdir(parents=True)
        revision = self.project.revision(source_a)
        original = dict(title='Existing A', canonicalKey='existing-a', suggestedType='entity', summary='Original fact.',
                        sources=[{k: revision[k] for k in ('kind', 'path', 'collection')}], sourceRevisions=[revision])
        page = compiler.markdown_page(original, original['summary'], 'generated', [], compiler.source_hash(original))
        self.card.write_text(page + '\n## Manual notes\nPreserve these notes.\n')
        decision = {'state': 'candidate', 'projectRoot': str(self.project.root),
                    'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                    'recallSettings': {'skipPaths': []},
                    'collections': [{'name': self.project.raw_name, 'role': 'raw', 'sourceRoot': str(self.project.root)},
                                    {'name': self.project.wiki_name, 'role': 'wiki',
                                     'sourceRoot': str(self.project.wiki)}]}
        self.bound = (decision, {'productRouteAllowed': True})

    def compile(self, title, key, source, revisions=None):
        candidate = {'trigger': 'post_tool_source', 'engine': 'claude', 'title': title,
                     'canonicalKey': key, 'summary': 'Revised synthetic fact.',
                     'suggestedType': 'entity', 'confidence': 'high',
                     'targetPath': '.memvane/wiki/entities/existing-a.md',
                     'sources': [{'kind': 'file', 'path': source, 'collection': self.project.raw_name}]}
        if revisions is None:
            revisions = [self.project.revision(self.project.root / source)]
        candidate['sourceRevisions'] = revisions
        before = {row['jobId'] for row in verify_rows(self.project.root)}
        visible = self.card.read_bytes()
        output = io.StringIO()
        with workspace.card_write_lock(self.project.root), workspace.mutation_lock(self.project.root), \
             patch.object(workspace, 'selected', return_value=self.bound), \
             patch.object(workspace, 'recheck'), patch.object(workspace, 'request_sync'), \
             patch.object(workspace, 'append_activity'), redirect_stdout(output):
            compiler._compile_locked(self.project.root, self.config, self.config['compile'],
                                     self.project.wiki, candidate, False)
        result = json.loads(output.getvalue()) if output.getvalue().strip() else {}
        self.assertEqual(self.card.read_bytes(), visible)
        if result.get('action') == 'updated':
            row, = (r for r in verify_rows(self.project.root) if r['jobId'] not in before)
            job = row['payload']
            self.assertEqual((job['kind'], job['targetPath']), ('replace', candidate['targetPath']))
            self.assertEqual(job['sourceRevisions'], candidate['sourceRevisions'])
            self.assertEqual(job['expectedTargetHash'], hashlib.sha256(visible).hexdigest())
            self.assertEqual([p.name for p in self.card.parent.iterdir() if p.name.startswith('.')], [])
            self.staged = row['image'].decode()
            self.assertIn('sourceHash="' + job['sourceHash'] + '"', self.staged)
        return result

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
        for title, key, source in (('Renamed A', 'existing-a', 'docs/b.md'),
                                   ('Different label', 'different-label', 'docs/a.md')):
            with self.subTest(key=key):
                result = self.compile(title, key, source)
                self.assertEqual(result['action'], 'updated')
                self.assertIn('Revised synthetic fact.', self.staged)
                self.assertIn('Preserve these notes.', self.staged)

    def test_refresh_ignores_reviewed_scalar_and_removes_old_verification_spans(self):
        old = self.card.read_text().replace('status: generated\n',
                                          'status: generated\nreviewed: true\nsourceSpans:\n'
                                          '  - {path: "docs/a.md", startLine: 1, endLine: 1}\n')
        self.card.write_text(old)
        result = self.compile('Renamed A', 'existing-a', 'docs/a.md')
        self.assertEqual(result['action'], 'updated')
        new = self.staged
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

    def test_disabled_verification_audits_existing_card_for_review(self):
        self.config['compile']['verify']['enabled'] = False
        self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md'), {})
        folder = self.project.root / '.memvane/compile'
        row, = [json.loads(raw) for raw in (folder / 'candidates.jsonl').read_bytes().splitlines()]
        self.assertEqual(row['targetPath'], self.card.relative_to(self.project.root).as_posix())
        self.assertEqual((row['action'], row['lint']['findings']), ('merge-needed', ['verified_replacement_requires_review']))
        self.assertEqual(verify_rows(self.project.root), [])

    def test_old_manual_status_is_refreshed_as_generated(self):
        self.card.write_text(self.card.read_text().replace('status: generated', 'status: manual\nreviewed: true'))
        self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md')['action'], 'updated')
        result = self.staged
        self.assertIn('status: generated', result)
        self.assertNotIn('reviewed:', result)
        self.assertIn('Revised synthetic fact.', result)

    def test_refresh_replaces_old_source_revision_with_current_bytes(self):
        source = self.project.root / 'docs/a.md'
        first = self.project.revision(source)
        self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md', [first])['action'], 'updated')
        source.write_text('updated synthetic source\n')
        second = self.project.revision(source)
        self.assertEqual(self.compile('Renamed A', 'existing-a', 'docs/a.md', [second])['action'], 'updated')
        block = compiler.FRONTMATTER_RE.match(self.staged).group(1)
        revisions = card.frontmatter_source_revisions(block)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertNotEqual(revisions[0]['sha256'], first['sha256'])
