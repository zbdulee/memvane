"""Writer decisions are reflected in durable audit and tombstone records."""
from contextlib import redirect_stdout
import io
import json
import hashlib
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as compiler
from memvane.wiki.card import api as card
from memvane.settings import api as settings
from memvane.wiki.workspace import api as workspace
from memvane.wiki.workspace import types as workspace_types
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class WriterFixture(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.project.write_note('docs/source.md', 'synthetic source\n')
        self.config = expanded(self.project, compile={'verify': {'enabled': False},
                               'semanticDedup': {'enabled': False}})
        use_config(self, self.project, self.config)
        self.bound = ({'state': 'candidate', 'projectRoot': str(self.project.root),
                       'installId': 'a' * 32, 'scopeId': 'b' * 32, 'storeIncarnation': 'c' * 32,
                       'recallSettings': {'skipPaths': []},
                       'collections': [{'name': self.project.wiki_name, 'role': 'wiki',
                                        'sourceRoot': str(self.project.wiki)}]}, {'productRouteAllowed': True})

    def compile(self, **changes):
        candidate = {'trigger': 'manual', 'title': 'Signal Rule', 'canonicalKey': 'signal-rule',
                     'summary': 'Synthetic durable decision.', 'suggestedType': 'concept',
                     'confidence': 'high', 'sources': [{'kind': 'file', 'path': 'docs/source.md'}]}
        candidate.update(changes)
        output = io.StringIO()
        with patch.object(workspace, 'selected', return_value=self.bound), \
             patch.object(workspace, 'recheck'), patch.object(workspace, 'request_sync'), \
             patch.object(workspace, 'append_activity'), redirect_stdout(output):
            compiler._compile_locked(self.project.root, self.config, self.config['compile'],
                                     self.project.wiki, candidate, False)
        text = output.getvalue().strip()
        return json.loads(text) if text else {}

    def audit(self, name):
        path = self.project.root / '.memvane/compile' / name
        return [json.loads(line) for line in path.read_text().splitlines()]


class WriterLifecycle(WriterFixture):
    def test_deleted_generated_identity_is_tombstoned_then_suppressed(self):
        first = self.compile()
        self.assertEqual(first['action'], 'created')
        self.assertFalse((self.project.root / '.memvane/compile/verify-queue.jsonl').exists())
        target = self.project.root / first['targetPath']
        target.unlink()
        self.assertEqual(self.compile(title='Renamed rule')['action'], 'tombstoned')
        self.assertEqual(self.compile(title='Another label')['action'], 'suppressed')
        self.assertFalse(target.exists())
        self.assertEqual(self.audit('tombstones.jsonl')[-1]['canonicalKey'], 'signal-rule')
        self.assertEqual(self.audit('candidates.jsonl')[-1]['action'], 'suppressed')

    def test_transcript_and_secret_are_rejected_without_persisting_raw_content(self):
        secret = 'sk-' + '1234567890abcdef' * 2
        result = self.compile(title='Unsafe', summary='User: private text\nAssistant: ok\n' + secret)
        self.assertEqual(result['action'], 'rejected')
        self.assertFalse((self.project.wiki / 'concepts/unsafe.md').exists())
        row = self.audit('candidates.jsonl')[-1]
        self.assertEqual(row['action'], 'rejected')
        self.assertNotIn(secret, json.dumps(row))
        self.assertNotIn('private text', json.dumps(row))

    def test_ambiguous_identity_is_audited_without_creating_or_overwriting(self):
        card = ('---\ntitle: "Signal"\ncanonicalKey: signal-rule\nstatus: generated\n'
                'createdBy: memvane\n---\nExisting card.\n')
        paths = [self.project.wiki / ('concepts/%s.md' % name) for name in ('first', 'second')]
        paths[0].parent.mkdir(parents=True)
        for path in paths:
            path.write_text(card)
        result = self.compile(title='Renamed identity')
        self.assertEqual((result['action'], result['reason']), ('merge-needed', 'ambiguous_canonicalKey'))
        self.assertEqual(len(result['targetMatches']), 2)
        self.assertEqual([path.read_text() for path in paths], [card, card])
        self.assertFalse((self.project.wiki / 'concepts/renamed-identity.md').exists())
        self.assertEqual(self.audit('candidates.jsonl')[-1]['action'], 'merge-needed')

    def test_ledger_path_refuses_symlink_without_touching_outside_file(self):
        directory = self.project.root / '.memvane/compile'
        directory.mkdir()
        outside = self.home / 'outside.jsonl'
        outside.write_text('unchanged')
        (directory / workspace_types.VERIFY_DELETED).symlink_to(outside)
        self.assertIsNone(settings.ledger(self.project.root, workspace_types.VERIFY_DELETED))
        self.assertEqual(settings.ledger(self.project.root, workspace_types.VERIFY_SKIPPED),
                         directory / workspace_types.VERIFY_SKIPPED)
        self.assertEqual(outside.read_text(), 'unchanged')

    def test_selected_verified_replacement_is_staged_for_review(self):
        self.config['compile']['verify']['enabled'] = True
        first = self.compile(summary='Original published fact.')
        target = self.project.root / first['targetPath']
        original = target.read_bytes()
        with patch.object(card, 'verified_wiki_row', return_value=True):
            result = self.compile(summary='Replacement fact for review.')
        self.assertEqual(result['action'], 'updated')
        self.assertEqual(target.read_bytes(), original)
        from tests.support.wiki_handoff import verify_rows
        row, = [row for row in verify_rows(self.project.root) if row['payload']['kind'] == 'replace']
        job = row['payload']
        self.assertEqual(job['expectedTargetHash'], hashlib.sha256(original).hexdigest())
        self.assertIn('sourceHash="' + job['sourceHash'] + '"', row['image'].decode())
        self.assertIn('Replacement fact for review.', row['image'].decode())

    def test_selected_replacement_refuses_unmatched_wiki_collection(self):
        first = self.compile()
        target = self.project.root / first['targetPath']
        original = target.read_bytes()
        self.bound[0]['collections'] = []
        self.assertEqual(self.compile(summary='Replacement fact.'), {})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.audit('candidates.jsonl')[-1]['lint']['findings'], ['selected_wiki_unresolved'])
