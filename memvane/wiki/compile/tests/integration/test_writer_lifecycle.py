"""Writer decisions are reflected in durable audit and tombstone records."""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

from memvane.wiki.compile import wiki_compile as compiler
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


class WriterLifecycle(FixtureCase):
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
                                     'auto-wiki', self.project.wiki, candidate, False)
        return json.loads(output.getvalue())

    def audit(self, name):
        path = self.project.root / '.memvane/compile' / name
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_deleted_generated_identity_is_tombstoned_then_suppressed(self):
        first = self.compile()
        self.assertEqual(first['action'], 'created')
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
